"""Тесты рассылки заданий: кому уходит, что происходит при блокировке бота."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError

from bot.db import Database
from bot.handlers.admin import dispatch_task

pytestmark = pytest.mark.asyncio


class StubBot:
    """Подменяет Bot: запоминает отправленное, умеет имитировать блокировку."""

    def __init__(self, blocked: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.blocked = blocked or set()

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        if chat_id in self.blocked:
            raise TelegramForbiddenError(
                method=None, message="Forbidden: bot was blocked by the user"
            )
        self.sent.append((chat_id, text))


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "dispatch.db")
    await database.connect()
    yield database
    await database.close()


async def add_performer(db: Database, tg_id: int, city: str, categories: list[str]) -> None:
    await db.touch_user(tg_id, f"u{tg_id}", f"U{tg_id}")
    await db.set_consent(tg_id)
    await db.save_profile(
        tg_id, city=city, age=30, has_car=False, contact="", categories=categories
    )


async def make_task(db: Database, city: str = "Москва", category: str = "АЗС") -> int:
    return await db.create_task(
        {
            "title": "Проверка АЗС",
            "category": category,
            "city": city,
            "payment": 1000,
            "deadline": "до пятницы",
            "slots": 2,
            "description": "Заправиться и оценить сервис",
            "checklist": "Чисто ли?",
        },
        created_by=1,
    )


async def test_dispatch_only_to_matching_performers(db: Database):
    await add_performer(db, 1, "Москва", ["АЗС"])
    await add_performer(db, 2, "Москва", ["Кафе и рестораны"])
    await add_performer(db, 3, "Казань", ["АЗС"])
    task_id = await make_task(db)

    bot = StubBot()
    sent, failed = await dispatch_task(bot, db, task_id)

    assert (sent, failed) == (1, 0)
    assert [chat_id for chat_id, _ in bot.sent] == [1]


async def test_dispatch_matches_city_written_differently(db: Database):
    """Человек написал «москва», задание создано на «Москва» — должно сойтись."""
    await add_performer(db, 10, "москва", ["АЗС"])
    task_id = await make_task(db, city="Москва ")

    bot = StubBot()
    sent, _ = await dispatch_task(bot, db, task_id)
    assert sent == 1


async def test_dispatch_marks_blocked_user(db: Database):
    await add_performer(db, 20, "Москва", ["АЗС"])
    await add_performer(db, 21, "Москва", ["АЗС"])
    task_id = await make_task(db)

    bot = StubBot(blocked={21})
    sent, failed = await dispatch_task(bot, db, task_id)

    assert (sent, failed) == (1, 1)
    # Заблокировавший бота помечается и больше не попадает в рассылки.
    assert (await db.get_user(21))["status"] == "blocked"
    assert [row["tg_id"] for row in await db.match_users("Москва", "АЗС")] == [20]


async def test_dispatch_shows_free_slots(db: Database):
    await add_performer(db, 30, "Москва", ["АЗС"])
    await add_performer(db, 31, "Москва", ["АЗС"])
    task_id = await make_task(db)
    await db.claim_task(task_id, 31)

    bot = StubBot()
    await dispatch_task(bot, db, task_id)

    # Один слот из двух уже занят.
    assert "Мест: 1 из 2" in bot.sent[0][1]


async def test_dispatch_no_matches_returns_zeros(db: Database):
    task_id = await make_task(db)
    bot = StubBot()
    assert await dispatch_task(bot, db, task_id) == (0, 0)
    assert bot.sent == []


async def test_dispatch_missing_task(db: Database):
    bot = StubBot()
    assert await dispatch_task(bot, db, 12345) == (0, 0)


async def test_dispatch_escapes_html_in_description(db: Database):
    """Описание от админа не должно ломать разметку сообщения."""
    await add_performer(db, 40, "Москва", ["АЗС"])
    task_id = await db.create_task(
        {
            "title": "Проверка",
            "category": "АЗС",
            "city": "Москва",
            "payment": 500,
            "deadline": "скоро",
            "slots": 1,
            "description": "Скидка <30%> & прочее",
            "checklist": "",
        },
        created_by=1,
    )

    bot = StubBot()
    await dispatch_task(bot, db, task_id)
    body = bot.sent[0][1]
    assert "&lt;30%&gt; &amp; прочее" in body
