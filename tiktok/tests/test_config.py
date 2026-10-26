"""Тесты чтения настроек: понятные ошибки вместо traceback при старте."""

from __future__ import annotations

import os
from datetime import tzinfo
from zoneinfo import ZoneInfo

import pytest

from tiktok_manager.config import DEFAULT_REDIRECT_URI, DIRECT, INBOX, load_config


def test_defaults():
    config = load_config()
    assert config.publish_mode == INBOX
    assert config.redirect_uri == DEFAULT_REDIRECT_URI
    assert isinstance(config.timezone, tzinfo)
    assert config.peak_hours == ()
    assert config.lead_minutes == 60
    assert config.max_posts_per_day == 2
    assert config.min_gap_hours == 3
    assert config.collect_every_minutes == 60
    assert config.db_path.name == "tiktok.db"


def test_publish_mode_is_case_insensitive():
    os.environ["PUBLISH_MODE"] = " Direct "
    assert load_config().publish_mode == DIRECT


def test_unknown_publish_mode():
    os.environ["PUBLISH_MODE"] = "auto"
    with pytest.raises(RuntimeError, match="PUBLISH_MODE"):
        load_config()


def test_peak_hours_in_any_notation():
    os.environ["PEAK_HOURS"] = "19, 13:00; 21,19"
    assert load_config().peak_hours == (19, 13, 21)


@pytest.mark.parametrize(
    ("raw", "message"),
    [("25", "от 0 до 23"), ("19:30", "целые часы"), ("вечер", "целые часы")],
)
def test_bad_peak_hours(raw, message):
    os.environ["PEAK_HOURS"] = raw
    with pytest.raises(RuntimeError, match=message):
        load_config()


def test_numbers_are_checked():
    os.environ["LEAD_MINUTES"] = "час"
    with pytest.raises(RuntimeError, match="целым числом"):
        load_config()
    os.environ["LEAD_MINUTES"] = "-5"
    with pytest.raises(RuntimeError, match="от 0 до 720"):
        load_config()


def test_collect_interval_too_rare_for_peaks():
    """Реже чем раз в два часа пиковые часы не посчитать."""
    os.environ["COLLECT_EVERY_MINUTES"] = "300"
    with pytest.raises(RuntimeError, match="COLLECT_EVERY_MINUTES"):
        load_config()


def test_timezone():
    os.environ["TIMEZONE"] = "Europe/Moscow"
    assert load_config().timezone == ZoneInfo("Europe/Moscow")


def test_unknown_timezone_explains_format():
    os.environ["TIMEZONE"] = "Москва"
    with pytest.raises(RuntimeError, match="Europe/Moscow"):
        load_config()


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/callback/",
        "http://localhost/callback/",
        "http://localhost:3455/callback/?x=1",
        "https://localhost:3455/callback/",
    ],
)
def test_redirect_uri_must_be_local_with_port(uri):
    os.environ["TIKTOK_REDIRECT_URI"] = uri
    with pytest.raises(RuntimeError, match="localhost"):
        load_config()


def test_redirect_uri_on_ip():
    os.environ["TIKTOK_REDIRECT_URI"] = "http://127.0.0.1:8080/cb/"
    assert load_config().redirect_uri == "http://127.0.0.1:8080/cb/"


def test_app_keys_required_only_when_asked():
    config = load_config()
    with pytest.raises(RuntimeError, match="TIKTOK_CLIENT_KEY"):
        config.require_app()
    os.environ["TIKTOK_CLIENT_KEY"] = "key"
    os.environ["TIKTOK_CLIENT_SECRET"] = "secret"
    load_config().require_app()


def test_env_file_from_current_folder(tmp_path):
    (tmp_path / ".env").write_text("PUBLISH_MODE=direct\nPEAK_HOURS=20\n", encoding="utf-8")
    config = load_config()
    assert config.publish_mode == DIRECT
    assert config.peak_hours == (20,)


def test_env_file_from_parent_folder_is_ignored(tmp_path, monkeypatch):
    """Уровнем выше лежит .env Telegram-бота — его DB_PATH нам чужой."""
    (tmp_path / ".env").write_text("DB_PATH=shopper.db\n", encoding="utf-8")
    inner = tmp_path / "tiktok"
    inner.mkdir()
    monkeypatch.chdir(inner)
    assert load_config().db_path.name == "tiktok.db"
