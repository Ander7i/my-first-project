"""Тесты напоминаний.

Главное требование: каждое напоминание уходит человеку ровно один раз.
Дубликаты раздражают сильнее, чем пропуски, и ломают доверие к боту.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio

from bot.config import Config
from bot.dates import to_iso
from bot.db import ACCEPTED, CANCELLED, REWORK, SUBMITTED, Database
from bot.reminders import _deadline_kind, is_quiet_hour, run_once
from tests.test_dispatch import StubBot

CONFIG = Config(
    token="1:x",
    admin_ids=(1,),
    group_chat_id=None,
    db_path=None,
    proxy_url=None,
    support_contact="@manager",
)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "rem.db")
    await database.connect()
    yield database
    await database.close()


async def add_performer(db: Database, tg_id: int, products: list[str]) -> None:
    await db.touch_user(tg_id, f"u{tg_id}", f"U{tg_id}")
    await db.set_consent(tg_id)
    await db.save_profile(
        tg_id,
        city="Москва",
        age=30,
        has_car=False,
        has_ip=False,
        contact="",
        products=products,
    )


async def make_task(db: Database, *, days_left: int, check_code="mfo_support") -> int:
    due = datetime.now(timezone.utc).date() + timedelta(days=days_left)
    return await db.create_task(
        {
            "check_code": check_code,
            "bank": "Тестбанк",
            "title": "Проверка",
            "city": "",
            "remote": True,
            "payment": 900,
            "deadline": "скоро",
            "due_date": to_iso(due),
            "slots": 3,
            "notes": "",
            "link": "",
        },
        created_by=1,
    )


# ------------------------------------------------------- выбор напоминания


def test_deadline_kind_steps():
    assert _deadline_kind(3) == "d3"
    assert _deadline_kind(1) == "d1"
    assert _deadline_kind(0) == "d0"


def test_deadline_kind_silent_days():
    """В промежуточные дни бот молчит, чтобы не надоедать."""
    for days in (10, 5, 4, 2):
        assert _deadline_kind(days) is None, days


def test_overdue_only_once_on_next_day():
    assert _deadline_kind(-1) == "overdue"
    # Дальше не напоминаем вовсе: человек уже всё понял.
    for days in (-2, -5, -30):
        assert _deadline_kind(days) is None, days


def test_quiet_hours_cover_night_moscow_time():
    # 23:00 МСК = 20:00 UTC — тишина.
    assert is_quiet_hour(datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc))
    # 03:00 МСК = 00:00 UTC — тишина.
    assert is_quiet_hour(datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc))
    # 13:00 МСК = 10:00 UTC — можно писать.
    assert not is_quiet_hour(datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc))


# ------------------------------------------------------- напоминания о сроке


async def test_reminder_sent_three_days_before(db: Database):
    await add_performer(db, 1, ["mfo"])
    task_id = await make_task(db, days_left=3)
    await db.claim_task(task_id, 1)

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 1
    assert "через 3 дн." in bot.sent[0][1]


async def test_reminder_wording_for_today_and_tomorrow(db: Database):
    await add_performer(db, 2, ["mfo"])
    await add_performer(db, 3, ["mfo"])
    tomorrow = await make_task(db, days_left=1)
    today = await make_task(db, days_left=0)
    await db.claim_task(tomorrow, 2)
    await db.claim_task(today, 3)

    bot = StubBot()
    await run_once(bot, db, CONFIG)
    texts_sent = {chat_id: text for chat_id, text in bot.sent}
    assert "завтра" in texts_sent[2]
    assert "сегодня" in texts_sent[3]


async def test_reminder_is_sent_only_once(db: Database):
    await add_performer(db, 4, ["mfo"])
    task_id = await make_task(db, days_left=1)
    await db.claim_task(task_id, 4)

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 1
    # Второй проход в тот же день не должен ничего прислать.
    assert await run_once(bot, db, CONFIG) == 0
    assert len(bot.sent) == 1


async def test_overdue_reminder(db: Database):
    await add_performer(db, 5, ["mfo"])
    task_id = await make_task(db, days_left=-1)
    await db.claim_task(task_id, 5)

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 1
    assert "прошёл" in bot.sent[0][1]


async def test_no_reminder_on_silent_day(db: Database):
    await add_performer(db, 6, ["mfo"])
    task_id = await make_task(db, days_left=5)
    await db.claim_task(task_id, 6)

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 0


async def test_submitted_task_gets_no_reminder(db: Database):
    """Отчёт сдан — напоминать не о чем."""
    await add_performer(db, 7, ["mfo"])
    task_id = await make_task(db, days_left=1)
    assignment_id, _ = await db.claim_task(task_id, 7)
    await db.set_assignment_status(assignment_id, SUBMITTED)

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 0


async def test_rework_task_still_gets_reminder(db: Database):
    """На доработке срок по-прежнему актуален."""
    await add_performer(db, 8, ["mfo"])
    task_id = await make_task(db, days_left=1)
    assignment_id, _ = await db.claim_task(task_id, 8)
    await db.set_assignment_status(assignment_id, REWORK, note="дослать фото")

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 1


async def test_dropped_task_gets_no_reminder(db: Database):
    await add_performer(db, 9, ["mfo"])
    task_id = await make_task(db, days_left=1)
    assignment_id, _ = await db.claim_task(task_id, 9)
    await db.set_assignment_status(assignment_id, CANCELLED)

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 0


async def test_task_without_due_date_is_skipped(db: Database):
    """Старые задания без машинной даты не должны ломать проход."""
    await add_performer(db, 10, ["mfo"])
    task_id = await db.create_task(
        {
            "check_code": "mfo_support",
            "bank": "Б",
            "title": "Старое",
            "city": "",
            "remote": True,
            "payment": 100,
            "deadline": "когда-нибудь",
            "slots": 1,
            "notes": "",
        },
        created_by=1,
    )
    await db.claim_task(task_id, 10)

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 0


async def test_blocked_user_does_not_break_the_pass(db: Database):
    """Один заблокировавший бота не должен мешать остальным."""
    await add_performer(db, 11, ["mfo"])
    await add_performer(db, 12, ["mfo"])
    task_id = await make_task(db, days_left=1)
    await db.claim_task(task_id, 11)
    await db.claim_task(task_id, 12)

    bot = StubBot(blocked={11})
    assert await run_once(bot, db, CONFIG) == 1
    assert (await db.get_user(11))["status"] == "blocked"


# ------------------------------------------------ напоминание после сдачи


async def test_followup_reminds_to_close_ip(db: Database):
    await add_performer(db, 20, ["ip"])
    task_id = await make_task(db, days_left=10, check_code="ip_registration")
    assignment_id, _ = await db.claim_task(task_id, 20)
    await db.set_assignment_status(assignment_id, ACCEPTED)

    # Сдвигаем приёмку на 30 дней назад — срок напоминания (25) прошёл.
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    await db.conn.execute(
        "UPDATE assignments SET decided_at = ? WHERE id = ?", (old, assignment_id)
    )

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 1
    text = bot.sent[0][1]
    assert "закрыва" in text.lower()
    assert "Р26001" in text
    assert "@manager" in text


async def test_followup_waits_for_its_day(db: Database):
    await add_performer(db, 21, ["ip"])
    task_id = await make_task(db, days_left=10, check_code="ip_registration")
    assignment_id, _ = await db.claim_task(task_id, 21)
    await db.set_assignment_status(assignment_id, ACCEPTED)

    bot = StubBot()
    # Приёмка только что — до 25 дней ещё далеко.
    assert await run_once(bot, db, CONFIG) == 0


async def test_followup_sent_once(db: Database):
    await add_performer(db, 22, ["ip"])
    task_id = await make_task(db, days_left=10, check_code="ip_registration")
    assignment_id, _ = await db.claim_task(task_id, 22)
    await db.set_assignment_status(assignment_id, ACCEPTED)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    await db.conn.execute(
        "UPDATE assignments SET decided_at = ? WHERE id = ?", (old, assignment_id)
    )

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 1
    assert await run_once(bot, db, CONFIG) == 0


async def test_no_followup_for_checks_without_it(db: Database):
    """У проверки поддержки после сдачи дел не остаётся."""
    await add_performer(db, 23, ["mfo"])
    task_id = await make_task(db, days_left=10)
    assignment_id, _ = await db.claim_task(task_id, 23)
    await db.set_assignment_status(assignment_id, ACCEPTED)
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
    await db.conn.execute(
        "UPDATE assignments SET decided_at = ? WHERE id = ?", (old, assignment_id)
    )

    bot = StubBot()
    assert await run_once(bot, db, CONFIG) == 0
