"""Тесты публикации: отправка в TikTok, повторы без дублей, статусы."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import MSK, NOW
from fakes import OPEN_ID, FakeClient, write_mp4

from tiktok_manager import db as store
from tiktok_manager.api import LoginRequired, TikTokError
from tiktok_manager.db import PostSettings, from_iso
from tiktok_manager.publisher import caption_length, check_status, publish


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def queue_video(db, account, tmp_path):
    def make(*, mode="direct", seconds=15.0, publish_at=NOW, **settings):
        path = write_mp4(tmp_path / "clip.mp4", seconds)
        options = {"caption": "Текст #тег", "privacy_level": "PUBLIC_TO_EVERYONE", **settings}
        item_id = db.add_item(
            open_id=OPEN_ID,
            path=str(path),
            mode=mode,
            settings=PostSettings(**options),
            publish_at=publish_at,
            auto_slot=True,
        )
        return db.item(item_id)

    return make


def sent(db, item, *, started=NOW, status=store.PROCESSING):
    """Ролик, уже отправленный в TikTok."""
    db.claim(item["id"], started)
    db.set_publish_id(item["id"], "pub-1", started)
    if status != store.UPLOADING:
        db.set_status(item["id"], status, started)
    return db.item(item["id"])


def run_publish(client, db, item, now=NOW):
    publish(item, client=client, db=db, now=now)
    return db.item(item["id"])


def test_direct_publish(client, db, queue_video):
    item = queue_video(allow_comment=True, allow_duet=True, allow_stitch=True, is_aigc=True)
    result = run_publish(client, db, item)

    assert result["status"] == store.PROCESSING
    assert result["publish_id"] == "pub-1"
    assert client.names() == ["creator_info", "init_post", "upload"]
    _, mode, post_info = client.calls[1]
    assert mode == "direct"
    assert post_info == {
        "title": "Текст #тег",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "disable_comment": False,
        # Дуэты выключены в настройках аккаунта — через API их не включить.
        "disable_duet": True,
        "disable_stitch": False,
        "brand_organic_toggle": False,
        "brand_content_toggle": False,
        "is_aigc": True,
    }


def test_interactions_are_off_unless_chosen(client, db, queue_video):
    run_publish(client, db, queue_video())
    post_info = client.calls[1][2]
    assert post_info["disable_comment"] and post_info["disable_duet"]
    assert post_info["disable_stitch"]


def test_inbox_mode_sends_only_the_file(client, db, queue_video):
    result = run_publish(client, db, queue_video(mode="inbox"))
    assert result["status"] == store.PROCESSING
    assert client.names() == ["init_post", "upload"]
    assert client.calls[0][1:] == ("inbox", None)


def test_unavailable_privacy_fails(client, db, queue_video):
    result = run_publish(client, db, queue_video(privacy_level="FOLLOWER_OF_CREATOR"))
    assert result["status"] == store.FAILED
    assert "Доступны: PUBLIC_TO_EVERYONE" in result["note"]
    assert "init_post" not in client.names()


def test_too_long_video_fails(client, db, queue_video):
    client.creator["max_video_post_duration_sec"] = 60
    result = run_publish(client, db, queue_video(seconds=90))
    assert result["status"] == store.FAILED
    assert "не длиннее 60 с" in result["note"]


def test_missing_file_fails(client, db, queue_video, tmp_path):
    item = queue_video()
    (tmp_path / "clip.mp4").unlink()
    result = run_publish(client, db, item)
    assert result["status"] == store.FAILED
    assert "не найден" in result["note"]


def test_temporary_error_retries_soon(client, db, queue_video):
    client.errors["init_post"] = TikTokError("rate_limit_exceeded")
    result = run_publish(client, db, queue_video())
    assert result["status"] == store.SCHEDULED
    assert from_iso(result["publish_at"]) == NOW + timedelta(minutes=5)
    assert result["attempts"] == 1
    assert "Слишком много запросов" in result["note"]


def test_temporary_error_gives_up_after_attempts(client, db, queue_video):
    item = queue_video()
    db.reschedule(item["id"], NOW, NOW, attempts=2)
    client.errors["init_post"] = TikTokError("internal_error")
    result = run_publish(client, db, db.item(item["id"]))
    assert result["status"] == store.FAILED
    assert "3 попытки" in result["note"]


def test_permanent_error_fails_with_explanation(client, db, queue_video):
    client.errors["creator_info"] = TikTokError("spam_risk_user_banned_from_posting")
    result = run_publish(client, db, queue_video())
    assert result["status"] == store.FAILED
    assert "запретил" in result["note"]


def test_login_required_keeps_item_in_queue(client, db, queue_video):
    client.errors["creator_info"] = LoginRequired()
    item = queue_video()
    with pytest.raises(LoginRequired):
        publish(item, client=client, db=db, now=NOW)
    assert db.item(item["id"])["status"] == store.SCHEDULED


def test_item_claimed_elsewhere_is_skipped(client, db, queue_video):
    item = queue_video()
    db.claim(item["id"], NOW)
    publish(item, client=client, db=db, now=NOW)
    assert client.calls == []


def test_upload_failure_is_not_retried_blindly(client, db, queue_video):
    """Ответ на последнюю часть мог потеряться — повтор вслепую дал бы дубль."""
    client.errors["upload"] = TikTokError("network_error")
    result = run_publish(client, db, queue_video())
    assert result["status"] == store.UPLOADING
    assert result["publish_id"] == "pub-1"


def check(client, db, config, item, now):
    check_status(item, client=client, db=db, config=config, now=now)
    return db.item(item["id"])


def test_published(client, db, config, queue_video):
    item = sent(db, queue_video())
    client.statuses = [{"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7301]}]
    result = check(client, db, config, item, NOW + timedelta(minutes=2))
    assert result["status"] == store.PUBLISHED
    assert result["post_id"] == "7301"


def test_inbox_draft_delivered(client, db, config, queue_video):
    item = sent(db, queue_video(mode="inbox"))
    client.statuses = [{"status": "SEND_TO_USER_INBOX"}]
    assert check(client, db, config, item, NOW)["status"] == store.INBOX_SENT


def test_rejected_by_tiktok(client, db, config, queue_video):
    item = sent(db, queue_video())
    client.statuses = [{"status": "FAILED", "fail_reason": "spam_risk_text"}]
    result = check(client, db, config, item, NOW)
    assert result["status"] == store.FAILED
    assert "спамом" in result["note"]


def test_tiktok_glitch_moves_to_next_peak(client, db, config, queue_video):
    item = sent(db, queue_video())
    client.statuses = [{"status": "FAILED", "fail_reason": "internal"}]
    result = check(client, db, config, item, NOW)
    assert result["status"] == store.SCHEDULED
    assert result["publish_id"] == ""
    assert result["attempts"] == 1
    # Своих данных нет — ближайший пик по умолчанию 19:00, публикация за час.
    assert from_iso(result["publish_at"]).astimezone(MSK).hour == 18


def test_unfinished_upload_is_resent_after_link_expires(client, db, config, queue_video):
    item = sent(db, queue_video(), status=store.UPLOADING)
    client.statuses = [{"status": "PROCESSING_UPLOAD"}, {"status": "PROCESSING_UPLOAD"}]
    assert check(client, db, config, item, NOW + timedelta(minutes=30))["status"] == store.UPLOADING
    result = check(client, db, config, item, NOW + timedelta(minutes=71))
    assert result["status"] == store.SCHEDULED
    assert result["attempts"] == 1


def test_processing_gives_up_after_a_day(client, db, config, queue_video):
    item = sent(db, queue_video())
    client.statuses = [{"status": "PROCESSING_DOWNLOAD"}]
    result = check(client, db, config, item, NOW + timedelta(hours=25))
    assert result["status"] == store.FAILED


def test_status_network_error_waits(client, db, config, queue_video):
    item = sent(db, queue_video())
    client.errors["publish_status"] = TikTokError("network_error")
    assert check(client, db, config, item, NOW)["status"] == store.PROCESSING


def test_unknown_publish_id_fails(client, db, config, queue_video):
    item = sent(db, queue_video())
    client.errors["publish_status"] = TikTokError("invalid_publish_id")
    assert check(client, db, config, item, NOW)["status"] == store.FAILED


def test_stale_claim_returns_to_queue(client, db, config, queue_video):
    """Процесс упал после захвата, но до запроса в TikTok."""
    item = queue_video()
    db.claim(item["id"], NOW)
    claimed = db.item(item["id"])
    assert check(client, db, config, claimed, NOW + timedelta(minutes=5))["status"] == (
        store.UPLOADING
    )
    assert check(client, db, config, claimed, NOW + timedelta(minutes=11))["status"] == (
        store.SCHEDULED
    )
    assert client.calls == []


def test_caption_length_counts_utf16():
    """Эмодзи вне базовой плоскости TikTok считает за два символа."""
    assert caption_length("Привет") == 6
    assert caption_length("🔥") == 2
