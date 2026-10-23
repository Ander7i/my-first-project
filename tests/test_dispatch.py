"""Тесты рассылки заданий: кому уходит, что при блокировке бота."""

from __future__ import annotations

import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError

from bot.db import Database
from bot.handlers.admin import _describe_filters, dispatch_task


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


async def add_performer(
    db: Database,
    tg_id: int,
    city: str,
    products: list[str],
    *,
    has_ip: bool = False,
) -> None:
    await db.touch_user(tg_id, f"u{tg_id}", f"U{tg_id}")
    await db.set_consent(tg_id)
    await db.save_profile(
        tg_id,
        city=city,
        age=30,
        has_car=False,
        has_ip=has_ip,
        contact="",
        products=products,
    )


async def make_task(
    db: Database,
    *,
    check_code: str = "debit_courier",
    city: str = "Москва",
    remote: bool = False,
    slots: int = 2,
    notes: str = "",
) -> int:
    return await db.create_task(
        {
            "check_code": check_code,
            "bank": "Тестбанк",
            "title": "Доставка карты — Тестбанк",
            "city": city,
            "remote": remote,
            "payment": 1000,
            "deadline": "до пятницы",
            "slots": slots,
            "notes": notes,
        },
        created_by=1,
    )


async def test_dispatch_only_to_matching_performers(db: Database):
    await add_performer(db, 1, "Москва", ["debit"])
    await add_performer(db, 2, "Москва", ["credit"])
    await add_performer(db, 3, "Казань", ["debit"])
    task_id = await make_task(db)

    bot = StubBot()
    sent, failed = await dispatch_task(bot, db, task_id)

    assert (sent, failed) == (1, 0)
    assert [chat_id for chat_id, _ in bot.sent] == [1]


async def test_dispatch_matches_city_written_differently(db: Database):
    """Человек написал «москва», задание создано на «Москва» — должно сойтись."""
    await add_performer(db, 10, "москва", ["debit"])
    task_id = await make_task(db, city="Москва ")

    bot = StubBot()
    sent, _ = await dispatch_task(bot, db, task_id)
    assert sent == 1


async def test_dispatch_remote_task_reaches_all_cities(db: Database):
    await add_performer(db, 11, "Москва", ["service"])
    await add_performer(db, 12, "Владивосток", ["service"])
    task_id = await make_task(db, check_code="support_phone", remote=True, city="")

    bot = StubBot()
    sent, _ = await dispatch_task(bot, db, task_id)
    assert sent == 2


async def test_dispatch_settlement_only_to_ip_owners(db: Database):
    await add_performer(db, 13, "Москва", ["settlement"], has_ip=True)
    await add_performer(db, 14, "Москва", ["settlement"], has_ip=False)
    task_id = await make_task(db, check_code="settlement_open", remote=True)

    bot = StubBot()
    sent, _ = await dispatch_task(bot, db, task_id)
    assert (sent, [c for c, _ in bot.sent]) == (1, [13])


async def test_dispatch_ip_registration_skips_existing_ip(db: Database):
    """Тому, у кого ИП уже есть, задание на регистрацию бессмысленно."""
    await add_performer(db, 15, "Москва", ["ip"], has_ip=True)
    await add_performer(db, 16, "Москва", ["ip"], has_ip=False)
    task_id = await make_task(db, check_code="ip_registration", remote=True)

    bot = StubBot()
    sent, _ = await dispatch_task(bot, db, task_id)
    assert (sent, [c for c, _ in bot.sent]) == (1, [16])


async def test_dispatch_marks_blocked_user(db: Database):
    await add_performer(db, 20, "Москва", ["debit"])
    await add_performer(db, 21, "Москва", ["debit"])
    task_id = await make_task(db)

    bot = StubBot(blocked={21})
    sent, failed = await dispatch_task(bot, db, task_id)

    assert (sent, failed) == (1, 1)
    # Заблокировавший бота помечается и больше не попадает в рассылки.
    assert (await db.get_user(21))["status"] == "blocked"
    assert [row["tg_id"] for row in await db.match_users("debit", city="Москва")] == [20]


async def test_dispatch_shows_free_slots(db: Database):
    await add_performer(db, 30, "Москва", ["debit"])
    await add_performer(db, 31, "Москва", ["debit"])
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


async def test_dispatch_unknown_check_code(db: Database):
    """Задание на удалённый из каталога вид проверки не рассылается."""
    await add_performer(db, 40, "Москва", ["debit"])
    task_id = await make_task(db, check_code="больше_не_существует")
    bot = StubBot()
    assert await dispatch_task(bot, db, task_id) == (0, 0)


async def test_dispatch_escapes_html_in_notes(db: Database):
    """Детали от админа не должны ломать разметку сообщения."""
    await add_performer(db, 41, "Москва", ["debit"])
    task_id = await make_task(db, notes="Тариф <Премиум> & доставка")

    bot = StubBot()
    await dispatch_task(bot, db, task_id)
    assert "&lt;Премиум&gt; &amp; доставка" in bot.sent[0][1]


def test_describe_filters_explains_empty_match():
    from bot.catalog import check_by_code

    text = _describe_filters(
        check_by_code("settlement_open"), {"city": "Казань", "remote": True}
    )
    assert "Расчётный счёт" in text
    assert "нужно действующее ИП" in text

    text = _describe_filters(
        check_by_code("debit_courier"), {"city": "Казань", "remote": False}
    )
    assert "Казань" in text
