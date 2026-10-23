"""Тесты чтения настроек: понятные ошибки вместо traceback при старте."""

from __future__ import annotations

import pytest

from bot.config import DEFAULT_CATEGORIES, load_config

ENV_VARS = (
    "BOT_TOKEN",
    "ADMIN_IDS",
    "GROUP_CHAT_ID",
    "DB_PATH",
    "CATEGORIES",
    "PROXY_URL",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Убирает переменные окружения и .env из рабочей папки."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_missing_token_explains_what_to_do():
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        load_config()


def test_missing_admins_explains_myid(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    with pytest.raises(RuntimeError, match="/myid"):
        load_config()


def test_non_numeric_admin_id_is_runtime_error(monkeypatch):
    """Не ValueError: иначе пользователь увидит traceback вместо подсказки."""
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "abc")
    with pytest.raises(RuntimeError, match="должен быть числом"):
        load_config()


def test_several_admins(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", " 111 , 222,333 ")
    assert load_config().admin_ids == (111, 222, 333)


def test_defaults(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "1")
    config = load_config()
    assert config.group_chat_id is None
    assert config.categories == DEFAULT_CATEGORIES
    assert config.proxy_url is None
    assert config.db_path.name == "shopper.db"


def test_custom_categories(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("CATEGORIES", "Магазины, Кафе ,, АЗС ")
    assert load_config().categories == ("Магазины", "Кафе", "АЗС")


def test_group_chat_id_is_negative(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("GROUP_CHAT_ID", "-1001234567890")
    assert load_config().group_chat_id == -1001234567890


def test_proxy_url_wins_over_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://from-env:3128")
    monkeypatch.setenv("PROXY_URL", "http://explicit:3128")
    assert load_config().proxy_url == "http://explicit:3128"


def test_proxy_falls_back_to_https_proxy(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://from-env:3128")
    assert load_config().proxy_url == "http://from-env:3128"


def test_is_admin(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "111,222")
    config = load_config()
    assert config.is_admin(111)
    assert config.is_admin(222)
    assert not config.is_admin(333)
