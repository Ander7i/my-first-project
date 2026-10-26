"""Тесты команд: разбор времени, выбор аккаунта, постановка роликов в очередь."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import MSK, NOW
from fakes import OPEN_ID, FakeClient, make_tokens, write_mp4

from tiktok_manager import cli
from tiktok_manager import db as store
from tiktok_manager.cli import main, parse_when, pick_account
from tiktok_manager.db import Database, from_iso


def msk(day: int, hour: int, minute: int = 0, month: int = 10, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK).astimezone(timezone.utc)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("19:00", msk(26, 19)),
        ("11:30", msk(27, 11, 30)),  # сегодня уже прошло — значит завтра
        ("завтра 9:05", msk(27, 9, 5)),
        ("сегодня 23:59", msk(26, 23, 59)),
        ("30.10 19:00", msk(30, 19)),
        ("01.03 10:00", msk(1, 10, month=3, year=2027)),  # дата без года — ближайшая
        ("30.10.2026 19:00", msk(30, 19)),
        ("2026-11-02 08:00", msk(2, 8, month=11)),
        ("  Завтра   9:05 ", msk(27, 9, 5)),
    ],
)
def test_parse_when(raw, expected):
    assert parse_when(raw, NOW, MSK) == expected


@pytest.mark.parametrize("raw", ["вечером", "25:00", "31.02 10:00", "19", "2026-13-01 10:00"])
def test_parse_when_rejects_garbage(raw):
    with pytest.raises(RuntimeError, match="Не понял время"):
        parse_when(raw, NOW, MSK)


def test_parse_when_rejects_past():
    with pytest.raises(RuntimeError, match="уже прошло"):
        parse_when("сегодня 10:00", NOW, MSK)
    with pytest.raises(RuntimeError, match="уже прошло"):
        parse_when("20.10.2026 10:00", NOW, MSK)


def test_pick_account(db, account):
    assert pick_account(db, None)["open_id"] == OPEN_ID
    assert pick_account(db, "@мой КАНАЛ")["open_id"] == OPEN_ID
    db.save_tokens(make_tokens(NOW, open_id="open-2"))
    assert pick_account(db, "open-2")["open_id"] == "open-2"
    with pytest.raises(RuntimeError, match="несколько аккаунтов"):
        pick_account(db, None)
    with pytest.raises(RuntimeError, match="не найден"):
        pick_account(db, "чужой")


def test_pick_account_without_login(db):
    with pytest.raises(RuntimeError, match="login"):
        pick_account(db, None)


@pytest.fixture
def setup_cli(tmp_path, monkeypatch):
    """Настройки через окружение и аккаунт в базе, время заморожено на NOW."""
    os.environ.update(
        {
            "TIKTOK_CLIENT_KEY": "key",
            "TIKTOK_CLIENT_SECRET": "secret",
            "TIMEZONE": "Europe/Moscow",
            "DB_PATH": str(tmp_path / "cli.db"),
        }
    )
    monkeypatch.setattr(cli, "utcnow", lambda: NOW)
    database = Database(tmp_path / "cli.db")
    database.save_tokens(make_tokens(NOW))
    database.set_display_name(OPEN_ID, "Мой канал")
    database.close()

    def open_db():
        return Database(tmp_path / "cli.db")

    return open_db


def items(open_db):
    database = open_db()
    try:
        return database.items()
    finally:
        database.close()


def test_add_picks_peak_slot(setup_cli, tmp_path, capsys):
    video = write_mp4(tmp_path / "clip.mp4")
    assert main(["add", str(video), "-c", "  Первый ролик #тест "]) == 0
    out = capsys.readouterr().out
    assert "#1 clip.mp4 → пн 26.10 18:00 (пик по умолчанию" in out
    assert "«Входящие»" in out
    (item,) = items(setup_cli)
    assert item["mode"] == "inbox"
    assert item["caption"] == "Первый ролик #тест"
    assert item["auto_slot"] == 1


def test_add_folder_uses_sidecar_captions(setup_cli, tmp_path):
    folder = tmp_path / "videos"
    folder.mkdir()
    write_mp4(folder / "a.mp4")
    write_mp4(folder / "b.mov")
    (folder / "a.txt").write_text("Подпись из файла\n#тег", encoding="utf-8")
    (folder / "notes.md").write_text("не видео", encoding="utf-8")
    assert main(["add", str(folder)]) == 0
    queued = {Path(item["path"]).name: item for item in items(setup_cli)}
    assert set(queued) == {"a.mp4", "b.mov"}
    assert queued["a.mp4"]["caption"] == "Подпись из файла\n#тег"
    assert queued["b.mov"]["caption"] == ""
    # Пики по умолчанию — 19:00 и 13:00, публикация за час. Сегодня 12:00 уже
    # не успеть: первый ролик выйдет в 18:00, второй — завтра в 12:00.
    assert from_iso(queued["a.mp4"]["publish_at"]) == msk(26, 18)
    assert from_iso(queued["b.mov"]["publish_at"]) == msk(27, 12)


def test_add_at_manual_time(setup_cli, tmp_path):
    video = write_mp4(tmp_path / "clip.mp4")
    assert main(["add", str(video), "--at", "завтра 21:15"]) == 0
    (item,) = items(setup_cli)
    assert from_iso(item["publish_at"]) == msk(27, 21, 15)
    assert item["auto_slot"] == 0


def test_add_rejects_bad_files_before_queueing_any(setup_cli, tmp_path, capsys):
    good = write_mp4(tmp_path / "good.mp4")
    assert main(["add", str(good), str(tmp_path / "missing.mp4")]) == 1
    assert "не найден" in capsys.readouterr().err
    assert items(setup_cli) == []


def test_add_rejects_long_caption(setup_cli, tmp_path, capsys):
    video = write_mp4(tmp_path / "clip.mp4")
    assert main(["add", str(video), "-c", "я" * 2201]) == 1
    assert "до 2200" in capsys.readouterr().err


def test_direct_mode_requires_explicit_privacy(setup_cli, tmp_path, capsys):
    os.environ["PUBLISH_MODE"] = "direct"
    video = write_mp4(tmp_path / "clip.mp4")
    assert main(["add", str(video)]) == 1
    assert "--privacy" in capsys.readouterr().err


def test_direct_mode_checks_account_settings(setup_cli, tmp_path, capsys, monkeypatch):
    os.environ["PUBLISH_MODE"] = "direct"
    monkeypatch.setattr(cli, "_creator_preview", lambda *args: FakeClient().creator)
    video = write_mp4(tmp_path / "clip.mp4")
    assert main(["add", str(video), "--privacy", "public_to_everyone", "--allow-comments"]) == 0
    assert "в аккаунт «Мой канал»" in capsys.readouterr().out
    (item,) = items(setup_cli)
    assert item["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert item["allow_comment"] == 1


def test_direct_mode_paid_content_cannot_be_private(setup_cli, tmp_path, capsys):
    os.environ["PUBLISH_MODE"] = "direct"
    video = write_mp4(tmp_path / "clip.mp4")
    assert main(["add", str(video), "--privacy", "SELF_ONLY", "--paid"]) == 1
    assert "SELF_ONLY" in capsys.readouterr().err


def test_add_requires_publish_scope(setup_cli, tmp_path, capsys):
    database = setup_cli()
    database.save_tokens(make_tokens(NOW, scope="user.info.basic,video.list"))
    database.close()
    video = write_mp4(tmp_path / "clip.mp4")
    assert main(["add", str(video)]) == 1
    assert "video.upload" in capsys.readouterr().err


def test_queue_remove_retry(setup_cli, tmp_path, capsys):
    video = write_mp4(tmp_path / "clip.mp4")
    main(["add", str(video), "-c", "Ролик"])
    main(["add", str(video), "-c", "Второй"])
    database = setup_cli()
    database.fail(2, "TikTok счёл подпись спамом.", NOW)
    database.close()
    capsys.readouterr()

    assert main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "ждёт (пик)" in out
    assert "#2: TikTok счёл подпись спамом." in out

    assert main(["retry", "1"]) == 1  # не в ошибке
    assert main(["retry", "2"]) == 0
    assert items(setup_cli)[1]["status"] == store.SCHEDULED

    assert main(["remove", "1"]) == 0
    assert [item["id"] for item in items(setup_cli)] == [2]
    assert main(["remove", "99"]) == 1


def test_stats_without_data(setup_cli, capsys):
    assert main(["stats"]) == 0
    assert "пока нет" in capsys.readouterr().out


def test_stats_with_snapshots(setup_cli, capsys):
    database = setup_cli()
    created = int((NOW - timedelta(days=3)).timestamp())
    for hours, views, followers in ((-30, 1000, 90), (-2, 1400, 100), (0, 1500, 104)):
        database.add_snapshot(
            OPEN_ID,
            NOW + timedelta(hours=hours),
            {
                "follower_count": followers,
                "following_count": 1,
                "likes_count": 50,
                "video_count": 1,
            },
            [
                {
                    "id": "v1",
                    "title": "Мой ролик",
                    "create_time": created,
                    "view_count": views,
                    "like_count": 100,
                    "comment_count": 10,
                    "share_count": 5,
                }
            ],
        )
    database.close()
    assert main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "Подписчики" in out
    assert "+14" in out
    assert "Просмотры по дням" in out
    assert "Мой ролик" in out
    assert "7.7%" in out  # (100 + 10 + 5) / 1500


def test_peak_without_data_explains_defaults(setup_cli, capsys):
    assert main(["peak"]) == 0
    out = capsys.readouterr().out
    assert "Пока беру часы по умолчанию: 19:00, 13:00, 16:00" in out
    assert "Публикую до 2 роликов в день: в 12:00 и 18:00 — за 60 мин до пика" in out
    assert "Ближайшие свободные слоты: пн 26.10 18:00 · вт 27.10 12:00 · вт 27.10 18:00" in out


def test_accounts(setup_cli, capsys):
    assert main(["accounts"]) == 0
    out = capsys.readouterr().out
    assert "Мой канал" in out
    assert "direct, inbox" in out


def test_commands_needing_tiktok_require_keys(setup_cli, capsys):
    del os.environ["TIKTOK_CLIENT_KEY"]
    assert main(["collect"]) == 1
    assert "TIKTOK_CLIENT_KEY" in capsys.readouterr().err
