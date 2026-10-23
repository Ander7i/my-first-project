"""Тесты выдачи ссылок на задание.

Два режима: ссылка хранится в задании и выдаётся ботом, либо задание
отправляет человека к администратору. Негодная ссылка не должна попадать
на кнопку: Telegram в этом случае отклоняет сообщение целиком, и задание
не дойдёт ни до кого.
"""

from __future__ import annotations

import pytest_asyncio

from bot.db import Database
from bot.utils import contact_url, is_valid_link


# ------------------------------------------------------------ проверка ссылок


def test_valid_links():
    for value in (
        "https://example.com",
        "http://example.com",
        "https://mfo.ru/promo?ref=abc123",
        "https://sub.domain.example.com/path/to/page#anchor",
    ):
        assert is_valid_link(value), value


def test_invalid_links():
    for value in (
        "",
        "   ",
        "example.com",
        "www.example.com",
        "ссылка будет позже",
        "https://",
        "https:// example.com",
        "ftp://example.com/file",
        "tg://resolve?domain=x",
        "https://example.com с комментарием",
    ):
        assert not is_valid_link(value), value


def test_link_is_trimmed_before_check():
    assert is_valid_link("  https://example.com  ")


# --------------------------------------------------------- контакт менеджера


def test_contact_url_from_handle():
    assert contact_url("@manager") == "https://t.me/manager"
    assert contact_url("manager") == "https://t.me/manager"
    assert contact_url("  @my_manager_1 ") == "https://t.me/my_manager_1"


def test_contact_url_passes_through_links():
    assert contact_url("https://t.me/manager") == "https://t.me/manager"
    assert contact_url("tg://resolve?domain=manager") == "tg://resolve?domain=manager"


def test_contact_url_rejects_unusable():
    for value in ("", "   ", "@ник кириллицей", "+7 900 000-00-00", "@"):
        assert contact_url(value) is None, value


# ----------------------------------------------------------- хранение в базе


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "links.db")
    await database.connect()
    yield database
    await database.close()


async def make_task(db: Database, link: str = "") -> int:
    return await db.create_task(
        {
            "check_code": "mfo_support",
            "bank": "МФО Тест",
            "title": "Проверка",
            "city": "",
            "remote": True,
            "payment": 500,
            "deadline": "до пятницы",
            "slots": 1,
            "notes": "",
            "link": link,
        },
        created_by=1,
    )


async def test_link_is_stored_and_read_back(db: Database):
    task_id = await make_task(db, "https://mfo.ru/promo?ref=abc")
    task = await db.get_task(task_id)
    assert task["link"] == "https://mfo.ru/promo?ref=abc"


async def test_link_defaults_to_empty(db: Database):
    """Задание без ссылки — режим ручной выдачи."""
    task = await db.get_task(await make_task(db))
    assert task["link"] == ""


async def test_create_task_without_link_key(db: Database):
    """Старый вызов без ключа link не должен падать."""
    task_id = await db.create_task(
        {
            "check_code": "mfo_support",
            "bank": "Б",
            "title": "T",
            "city": "",
            "remote": True,
            "payment": 100,
            "deadline": "скоро",
            "slots": 1,
            "notes": "",
        },
        created_by=1,
    )
    assert (await db.get_task(task_id))["link"] == ""


async def test_assignment_carries_link(db: Database):
    """Ссылку надо отдавать повторно, поэтому она должна быть в заявке."""
    await db.touch_user(1, "u", "U")
    await db.set_consent(1)
    await db.save_profile(
        1,
        city="Москва",
        age=30,
        has_car=False,
        has_ip=False,
        contact="",
        products=["mfo"],
    )
    task_id = await make_task(db, "https://mfo.ru/x")
    assignment_id, code = await db.claim_task(task_id, 1)
    assert code == "ok"

    assignment = await db.get_assignment(assignment_id)
    assert assignment["link"] == "https://mfo.ru/x"


async def test_migration_adds_link_to_old_db(tmp_path):
    """В существующей базе колонка link должна долиться при старте."""
    import aiosqlite

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_code TEXT NOT NULL DEFAULT '',
                bank TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                city TEXT NOT NULL DEFAULT '',
                payment INTEGER NOT NULL,
                deadline TEXT NOT NULL,
                slots INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'open',
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            "INSERT INTO tasks (title, payment, deadline, created_by, created_at) "
            "VALUES ('Старое', 100, 'скоро', 1, '2026-01-01')"
        )
        await conn.commit()

    database = Database(path)
    await database.connect()
    try:
        task = await database.get_task(1)
        assert task["title"] == "Старое"
        assert task["link"] == ""
    finally:
        await database.close()
