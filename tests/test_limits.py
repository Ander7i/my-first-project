"""Тесты лимитов повторов.

Это защита человека, а не формальность: каждое повторение займа оставляет
запись в кредитной истории и приближает к законному лимиту на число
одновременных займов, после которого МФО обязано отказать.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot import catalog
from bot.db import CANCELLED, Database
from bot.handlers.admin import dispatch_task
from bot.utils import unsuitable_reason
from tests.test_dispatch import StubBot


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "limits.db")
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


async def make_task(db: Database, check_code: str) -> int:
    return await db.create_task(
        {
            "check_code": check_code,
            "bank": "МФО Тест",
            "title": "Проверка",
            "city": "",
            "remote": True,
            "payment": 2000,
            "deadline": "до пятницы",
            "slots": 5,
            "notes": "",
        },
        created_by=1,
    )


async def backdate_claim(db: Database, assignment_id: int, iso: str) -> None:
    """Сдвигает дату взятия назад, чтобы проверить окончание паузы."""
    await db.conn.execute(
        "UPDATE assignments SET claimed_at = ? WHERE id = ?", (iso, assignment_id)
    )


# ------------------------------------------------------------------ каталог


def test_loan_check_has_limits():
    """Задание с реальным займом обязано быть ограничено."""
    check = catalog.check_by_code("mfo_disbursement")
    assert check is not None
    assert check.needs_ack
    assert check.max_per_user > 0
    assert check.cooldown_days > 0


def test_every_ack_check_is_limited():
    """Правило каталога: нужно подтверждение — значит нужен и лимит."""
    for check in catalog.CHECKS:
        if check.needs_ack:
            assert check.cooldown_days or check.max_per_user, check.code


def test_validate_rejects_unlimited_ack_check():
    """Забытый лимит должен ронять бота при старте, а не доходить до людей."""
    broken = catalog.CheckType(
        code="x",
        product="mfo",
        title="X",
        summary="S",
        duration="D",
        remote=True,
        steps=("шаг",),
        requirements=("треб",),
        checklist=("вопрос",),
        needs_ack=True,
        ack_text="текст",
    )
    original = catalog.CHECKS
    catalog.CHECKS = (*original, broken)
    try:
        with pytest.raises(RuntimeError, match="cooldown_days"):
            catalog.validate()
    finally:
        catalog.CHECKS = original


def test_safe_mfo_checks_need_no_loan():
    """Два задания по МФО должны обходиться без займа и без подтверждения."""
    for code in ("mfo_support", "mfo_terms_online"):
        check = catalog.check_by_code(code)
        assert check is not None
        assert not check.needs_ack, code
        assert check.max_per_user == 0, code


def test_loan_check_discloses_the_three_traps():
    check = catalog.check_by_code("mfo_disbursement")
    joined = (" ".join(check.warnings) + check.ack_text).lower()
    # Условность нулевой ставки.
    assert "0,8%" in joined or "0.8%" in joined
    # Исключение из лимита переплаты — именно оно касается займов под 0%.
    assert "353-фз" in joined
    # Кредитная история остаётся даже при идеальном возврате.
    assert "кредитн" in joined and "ипотек" in joined
    # Законный лимит на число займов.
    assert "октября 2026" in joined
    # Долг нельзя переводить дальше.
    assert "долг останется" in joined or "долг останется на вас" in joined


def test_loan_check_warns_not_to_rely_on_task_payment():
    """Возврат своими деньгами: оплата приходит после проверки отчёта."""
    check = catalog.check_by_code("mfo_disbursement")
    joined = " ".join(check.requirements) + check.ack_text
    assert "рассчитывать" in joined.lower()


def test_terms_check_discloses_bki_inquiry():
    """Даже заявка без денег создаёт запрос в БКИ — это надо сказать."""
    check = catalog.check_by_code("mfo_terms_online")
    joined = " ".join(check.warnings).lower()
    assert "бки" in joined


# ------------------------------------------------- проверка при взятии задания


async def test_cooldown_blocks_second_claim(db: Database):
    await add_performer(db, 1, ["mfo"])
    first = await make_task(db, "mfo_disbursement")
    second = await make_task(db, "mfo_disbursement")
    await db.claim_task(first, 1)

    user = await db.get_user(1)
    task = await db.get_task(second)
    prior, days = await db.check_history(1, "mfo_disbursement")

    reason = unsuitable_reason(user, task, prior_count=prior, days_since_last=days)
    assert reason is not None
    assert "подряд" in reason


async def test_cooldown_expires(db: Database):
    await add_performer(db, 2, ["mfo"])
    first = await make_task(db, "mfo_disbursement")
    second = await make_task(db, "mfo_disbursement")
    assignment_id, _ = await db.claim_task(first, 2)
    await backdate_claim(db, assignment_id, "2020-01-01T00:00:00+00:00")

    user = await db.get_user(2)
    task = await db.get_task(second)
    prior, days = await db.check_history(2, "mfo_disbursement")

    assert days is not None and days > 45
    assert unsuitable_reason(user, task, prior_count=prior, days_since_last=days) is None


async def test_max_per_user_blocks_after_limit(db: Database):
    await add_performer(db, 3, ["mfo"])
    check = catalog.check_by_code("mfo_disbursement")

    # Берём задание столько раз, сколько разрешено, сдвигая даты в прошлое,
    # чтобы упереться именно в общий лимит, а не в паузу.
    for _ in range(check.max_per_user):
        task_id = await make_task(db, "mfo_disbursement")
        assignment_id, code = await db.claim_task(task_id, 3)
        assert code == "ok"
        await backdate_claim(db, assignment_id, "2020-01-01T00:00:00+00:00")

    extra = await make_task(db, "mfo_disbursement")
    user = await db.get_user(3)
    task = await db.get_task(extra)
    prior, days = await db.check_history(3, "mfo_disbursement")

    assert prior == check.max_per_user
    reason = unsuitable_reason(user, task, prior_count=prior, days_since_last=days)
    assert reason is not None
    assert "не больше" in reason


async def test_cancelled_assignment_does_not_count(db: Database):
    """Отменённое задание не должно расходовать лимит человека."""
    await add_performer(db, 4, ["mfo"])
    task_id = await make_task(db, "mfo_disbursement")
    assignment_id, _ = await db.claim_task(task_id, 4)
    await db.set_assignment_status(assignment_id, CANCELLED)

    prior, days = await db.check_history(4, "mfo_disbursement")
    assert (prior, days) == (0, None)


async def test_limits_are_per_check_type(db: Database):
    """Займ не должен блокировать безопасные задания по тому же продукту."""
    await add_performer(db, 5, ["mfo"])
    loan = await make_task(db, "mfo_disbursement")
    await db.claim_task(loan, 5)

    support = await db.get_task(await make_task(db, "mfo_support"))
    user = await db.get_user(5)
    prior, days = await db.check_history(5, "mfo_support")

    assert (prior, days) == (0, None)
    assert unsuitable_reason(user, support, prior_count=prior, days_since_last=days) is None


async def test_history_without_limits_is_ignored(db: Database):
    """У проверки без лимитов история не должна ничего блокировать."""
    await add_performer(db, 6, ["mfo"])
    first = await make_task(db, "mfo_support")
    second = await make_task(db, "mfo_support")
    await db.claim_task(first, 6)

    user = await db.get_user(6)
    task = await db.get_task(second)
    prior, days = await db.check_history(6, "mfo_support")

    assert prior == 1
    assert unsuitable_reason(user, task, prior_count=prior, days_since_last=days) is None


# --------------------------------------------------- подбор и рассылка


async def test_blocked_task_is_hidden_from_available(db: Database):
    """Предлагать то, что нельзя взять, хуже, чем не предлагать."""
    await add_performer(db, 7, ["mfo"])
    first = await make_task(db, "mfo_disbursement")
    second = await make_task(db, "mfo_disbursement")
    await db.claim_task(first, 7)

    user = await db.get_user(7)
    rows = await db.open_tasks_for_user(user)
    assert second not in [row["id"] for row in rows]


async def test_dispatch_skips_user_in_cooldown(db: Database):
    await add_performer(db, 8, ["mfo"])
    await add_performer(db, 9, ["mfo"])
    first = await make_task(db, "mfo_disbursement")
    await db.claim_task(first, 8)

    second = await make_task(db, "mfo_disbursement")
    bot = StubBot()
    sent, failed = await dispatch_task(bot, db, second)

    # Человеку в паузе задание не уходит, второму — уходит.
    assert (sent, failed) == (1, 0)
    assert [chat_id for chat_id, _ in bot.sent] == [9]


async def test_dispatch_reaches_everyone_without_limits(db: Database):
    await add_performer(db, 10, ["mfo"])
    await add_performer(db, 11, ["mfo"])
    first = await make_task(db, "mfo_support")
    await db.claim_task(first, 10)

    second = await make_task(db, "mfo_support")
    bot = StubBot()
    sent, _ = await dispatch_task(bot, db, second)
    assert sent == 2
