"""Тесты хранилища: токены, снимки статистики, очередь."""

from __future__ import annotations

from datetime import datetime, timedelta

from conftest import NOW
from fakes import OPEN_ID, make_tokens

from tiktok_manager import db as store
from tiktok_manager.db import PostSettings, from_iso


def video(video_id: str, views: int, *, created: datetime = NOW - timedelta(days=3), **extra):
    return {
        "id": video_id,
        "title": f"Ролик {video_id}",
        "create_time": int(created.timestamp()),
        "share_url": f"https://www.tiktok.com/@me/video/{video_id}",
        "duration": 15,
        "view_count": views,
        "like_count": views // 10,
        "comment_count": 1,
        "share_count": 0,
        **extra,
    }


def add(db, publish_at: datetime, *, status: str | None = None, **settings) -> int:
    item_id = db.add_item(
        open_id=OPEN_ID,
        path="/videos/clip.mp4",
        mode="inbox",
        settings=PostSettings(**settings),
        publish_at=publish_at,
        auto_slot=True,
    )
    if status:
        db.set_status(item_id, status, NOW)
    return item_id


def test_tokens_update_keeps_display_name(db, account):
    db.save_tokens(make_tokens(NOW, access="access-2", refresh="refresh-2"))
    row = db.account(OPEN_ID)
    assert row["access_token"] == "access-2"
    assert row["refresh_token"] == "refresh-2"
    assert row["display_name"] == "Мой канал"
    assert len(db.accounts()) == 1


def test_snapshot_saves_account_and_videos(db, account):
    user = {"follower_count": 100, "following_count": 5, "likes_count": 900, "video_count": 2}
    db.add_snapshot(OPEN_ID, NOW, user, [video("v1", 500), video("v2", 50)])
    db.add_snapshot(OPEN_ID, NOW + timedelta(hours=1), user, [video("v1", 650), video("v2", 60)])

    assert db.latest_account_stats(OPEN_ID)["follower_count"] == 100
    assert db.last_snapshot_at(OPEN_ID) == NOW + timedelta(hours=1)
    videos = {row["id"]: row for row in db.videos(OPEN_ID)}
    assert videos["v1"]["view_count"] == 650  # хранится последнее значение
    points = db.video_points(OPEN_ID, NOW)
    assert [(p["video_id"], p["view_count"]) for p in points] == [
        ("v1", 500),
        ("v1", 650),
        ("v2", 50),
        ("v2", 60),
    ]


def test_snapshot_without_stats_scope_skips_account_counters(db, account):
    """Без user.info.stats в ответе нет счётчиков — нули записывать нельзя."""
    db.add_snapshot(OPEN_ID, NOW, {"display_name": "Мой канал"}, [video("v1", 10)])
    assert db.latest_account_stats(OPEN_ID) is None
    assert db.last_snapshot_at(OPEN_ID) == NOW


def test_title_falls_back_to_description(db, account):
    db.add_snapshot(OPEN_ID, NOW, {}, [video("v1", 1, title="", video_description="Описание")])
    assert db.videos(OPEN_ID)[0]["title"] == "Описание"


def test_account_stats_at_prefers_snapshot_before_moment(db, account):
    for hours, followers in ((0, 10), (24, 20), (48, 30)):
        stats = {"follower_count": followers}
        db.add_snapshot(OPEN_ID, NOW + timedelta(hours=hours), stats, [])
    assert db.account_stats_at(OPEN_ID, NOW + timedelta(hours=30))["follower_count"] == 20
    # Раньше первого снимка — берём самый ранний.
    assert db.account_stats_at(OPEN_ID, NOW - timedelta(days=5))["follower_count"] == 10


def test_queue_settings_round_trip(db, account):
    item_id = add(db, NOW, caption="Текст #тег", privacy_level="SELF_ONLY", allow_duet=True)
    item = db.item(item_id)
    assert item["status"] == store.SCHEDULED
    assert item["caption"] == "Текст #тег"
    assert item["privacy_level"] == "SELF_ONLY"
    assert item["allow_duet"] == 1
    assert item["allow_comment"] == 0
    assert from_iso(item["publish_at"]) == NOW


def test_due_returns_only_scheduled_and_ready(db, account):
    ready = add(db, NOW - timedelta(minutes=1))
    add(db, NOW + timedelta(hours=1))
    add(db, NOW - timedelta(hours=1), status=store.FAILED)
    assert [row["id"] for row in db.due(OPEN_ID, NOW)] == [ready]


def test_claim_is_exclusive(db, account):
    """Два запущенных планировщика не должны опубликовать один ролик дважды."""
    item_id = add(db, NOW)
    assert db.claim(item_id, NOW)
    assert not db.claim(item_id, NOW)
    assert db.item(item_id)["status"] == store.UPLOADING


def test_unclaim_returns_to_queue(db, account):
    item_id = add(db, NOW)
    db.claim(item_id, NOW)
    db.unclaim(item_id, NOW)
    assert db.item(item_id)["status"] == store.SCHEDULED


def test_publish_id_and_in_flight(db, account):
    item_id = add(db, NOW)
    db.claim(item_id, NOW)
    db.set_publish_id(item_id, "pub-1", NOW)
    item = db.item(item_id)
    assert item["publish_id"] == "pub-1"
    assert from_iso(item["started_at"]) == NOW
    assert [row["id"] for row in db.in_flight(OPEN_ID)] == [item_id]


def test_taken_slots_ignore_failed_and_excluded(db, account):
    first = add(db, NOW + timedelta(hours=2))
    add(db, NOW + timedelta(hours=5), status=store.FAILED)
    add(db, NOW - timedelta(hours=3), status=store.PUBLISHED)
    add(db, NOW - timedelta(days=3), status=store.PUBLISHED)
    taken = db.taken_slots(OPEN_ID, NOW - timedelta(days=1))
    assert sorted(taken) == [NOW - timedelta(hours=3), NOW + timedelta(hours=2)]
    assert db.taken_slots(OPEN_ID, NOW - timedelta(days=1), exclude_id=first) == [
        NOW - timedelta(hours=3)
    ]


def test_reschedule_clears_publish_state(db, account):
    item_id = add(db, NOW)
    db.claim(item_id, NOW)
    db.set_publish_id(item_id, "pub-1", NOW)
    db.reschedule(item_id, NOW + timedelta(days=1), NOW, attempts=2, note="повтор")
    item = db.item(item_id)
    assert item["status"] == store.SCHEDULED
    assert item["publish_id"] == ""
    assert item["started_at"] is None
    assert item["attempts"] == 2
    assert item["note"] == "повтор"


def test_delete_item(db, account):
    item_id = add(db, NOW)
    assert db.delete_item(item_id)
    assert not db.delete_item(item_id)
    assert db.item(item_id) is None
