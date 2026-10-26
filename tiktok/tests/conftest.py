"""Общие заготовки для тестов."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fakes import OPEN_ID, make_tokens

from tiktok_manager.config import INBOX, Config
from tiktok_manager.db import Database

MSK = ZoneInfo("Europe/Moscow")
# Понедельник, 12:00 по Москве.
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)

ENV_VARS = (
    "TIKTOK_CLIENT_KEY",
    "TIKTOK_CLIENT_SECRET",
    "TIKTOK_REDIRECT_URI",
    "PUBLISH_MODE",
    "TIMEZONE",
    "PEAK_HOURS",
    "LEAD_MINUTES",
    "MAX_POSTS_PER_DAY",
    "MIN_GAP_HOURS",
    "COLLECT_EVERY_MINUTES",
    "DB_PATH",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Своё окружение и рабочая папка на каждый тест.

    load_dotenv пишет прямо в os.environ, мимо monkeypatch, поэтому
    подменяем окружение целиком — иначе .env одного теста протёк бы в другие.
    """
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for name in ENV_VARS:
        os.environ.pop(name, None)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        client_key="client-key",
        client_secret="client-secret",
        redirect_uri="http://localhost:3455/callback/",
        publish_mode=INBOX,
        timezone=MSK,
        peak_hours=(),
        lead_minutes=60,
        max_posts_per_day=2,
        min_gap_hours=3,
        collect_every_minutes=60,
        db_path=tmp_path / "test.db",
    )


@pytest.fixture
def direct_config(config: Config) -> Config:
    return replace(config, publish_mode="direct")


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def account(db: Database):
    db.save_tokens(make_tokens(NOW))
    db.set_display_name(OPEN_ID, "Мой канал")
    return db.account(OPEN_ID)
