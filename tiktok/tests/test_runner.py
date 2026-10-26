"""Тесты планировщика: сбор статистики по расписанию, публикация, сбои."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import MSK, NOW
from fakes import OPEN_ID, FakeClient, make_tokens, write_mp4

from tiktok_manager import db as store
from tiktok_manager.api import LoginRequired, TikTokError
from tiktok_manager.db import PostSettings, from_iso
from tiktok_manager.runner import Runner


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def client():
    client = FakeClient()
    client.video_list = [
        {"id": "v1", "create_time": int((NOW - timedelta(days=2)).timestamp()), "view_count": 10}
    ]
    return client


@pytest.fixture
def runner(config, db, account, client, clock):
    return Runner(config, db, client, clock=clock)


def collects(client):
    return client.names().count("user_info")


def add_item(db, tmp_path, publish_at):
    path = write_mp4(tmp_path / "clip.mp4")
    item_id = db.add_item(
        open_id=OPEN_ID,
        path=str(path),
        mode="inbox",
        settings=PostSettings(caption="Текст"),
        publish_at=publish_at,
        auto_slot=True,
    )
    return item_id


def test_collects_on_start_and_then_on_schedule(runner, client, clock, db):
    runner.tick()
    assert collects(client) == 1
    assert db.last_snapshot_at(OPEN_ID) == NOW
    clock.advance(minutes=30)
    runner.tick()
    assert collects(client) == 1
    clock.advance(minutes=31)
    runner.tick()
    assert collects(client) == 2


def test_failed_collect_is_retried_soon(runner, client, clock):
    client.errors["user_info"] = TikTokError("internal_error")
    runner.tick()
    del client.errors["user_info"]
    clock.advance(minutes=4)
    runner.tick()
    assert collects(client) == 1
    clock.advance(minutes=2)
    runner.tick()
    assert collects(client) == 2


def test_publishes_due_item_and_follows_status(runner, client, clock, db, tmp_path):
    item_id = add_item(db, tmp_path, NOW - timedelta(minutes=1))
    runner.tick()
    assert db.item(item_id)["status"] == store.PROCESSING
    client.statuses = [{"status": "SEND_TO_USER_INBOX"}]
    clock.advance(minutes=1)
    runner.tick()
    assert db.item(item_id)["status"] == store.INBOX_SENT


def test_future_item_waits(runner, client, db, tmp_path):
    item_id = add_item(db, tmp_path, NOW + timedelta(hours=1))
    runner.tick()
    assert db.item(item_id)["status"] == store.SCHEDULED
    assert "init_post" not in client.names()


def test_missed_item_moves_to_next_peak(runner, client, db, tmp_path):
    """Компьютер спал в нужное время — публиковать с опозданием в «мёртвый» час не стоит."""
    item_id = add_item(db, tmp_path, NOW - timedelta(hours=2))
    runner.tick()
    item = db.item(item_id)
    assert item["status"] == store.SCHEDULED
    assert from_iso(item["publish_at"]) > NOW
    assert from_iso(item["publish_at"]).astimezone(MSK).hour == 18
    assert "пропущено" in item["note"]
    assert "init_post" not in client.names()


def test_login_required_pauses_account(runner, client, clock, caplog):
    client.errors["user_info"] = LoginRequired()
    runner.tick()
    assert "Войдите заново" in caplog.text
    clock.advance(minutes=30)
    runner.tick()
    assert collects(client) == 1
    clock.advance(minutes=31)
    runner.tick()
    assert collects(client) == 2


class BrokenForFirst(FakeClient):
    def user_info(self, open_id):
        if open_id == OPEN_ID:
            raise RuntimeError("что-то сломалось")
        return super().user_info(open_id)


def test_one_broken_account_does_not_stop_others(config, db, account, clock, caplog):
    db.save_tokens(make_tokens(NOW, open_id="open-2"))
    client = BrokenForFirst()
    Runner(config, db, client, clock=clock).tick()
    assert "непредвиденная ошибка" in caplog.text
    assert ("user_info", "open-2") in client.calls
    assert db.last_snapshot_at("open-2") == NOW
