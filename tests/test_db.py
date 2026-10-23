"""Тесты слоя базы: подбор исполнителей, занятие слотов, отчёты."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from bot.db import (
    ACCEPTED,
    CANCELLED,
    CLAIMED,
    PAID,
    REWORK,
    SUBMITTED,
    Database,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


async def add_performer(
    db: Database,
    tg_id: int,
    *,
    city: str = "Москва",
    categories: list[str] | None = None,
) -> None:
    await db.touch_user(tg_id, f"user{tg_id}", f"User {tg_id}")
    await db.set_consent(tg_id)
    await db.save_profile(
        tg_id,
        city=city,
        age=30,
        has_car=False,
        contact="",
        categories=categories or ["Кафе и рестораны"],
    )


async def make_task(db: Database, *, slots: int = 1, city: str = "Москва") -> int:
    return await db.create_task(
        {
            "title": "Проверка кафе",
            "category": "Кафе и рестораны",
            "city": city,
            "payment": 800,
            "deadline": "до 30 октября",
            "slots": slots,
            "description": "Зайти и оценить сервис",
            "checklist": "Сколько ждали?\nПоздоровались?",
        },
        created_by=1,
    )


# ------------------------------------------------------------------ профили


async def test_touch_user_is_idempotent(db: Database):
    await db.touch_user(100, "vasya", "Вася")
    await db.touch_user(100, "vasya_new", "Вася П")
    row = await db.get_user(100)
    assert row["username"] == "vasya_new"
    assert row["full_name"] == "Вася П"
    assert row["status"] == "new"


async def test_profile_activates_user(db: Database):
    await add_performer(db, 101)
    row = await db.get_user(101)
    assert row["status"] == "active"
    assert row["consent_at"] is not None
    assert row["city"] == "Москва"


async def test_delete_user_removes_everything(db: Database):
    await add_performer(db, 102)
    task_id = await make_task(db)
    assignment_id, code = await db.claim_task(task_id, 102)
    assert code == "ok"
    await db.add_report_item(assignment_id, "photo", file_id="abc")

    await db.delete_user(102)

    assert await db.get_user(102) is None
    # Заявка и отчёт уходят вместе с пользователем (ON DELETE CASCADE).
    assert await db.get_assignment(assignment_id) is None
    assert await db.list_report_items(assignment_id) == []


# ------------------------------------------------------------------- подбор


async def test_match_users_by_city_and_category(db: Database):
    await add_performer(db, 201, city="Москва", categories=["Кафе и рестораны"])
    await add_performer(db, 202, city="Казань", categories=["Кафе и рестораны"])
    await add_performer(db, 203, city="Москва", categories=["АЗС"])

    matched = await db.match_users("Москва", "Кафе и рестораны")
    assert [row["tg_id"] for row in matched] == [201]


async def test_match_users_ignores_case_and_spaces(db: Database):
    await add_performer(db, 204, city="  москва ")
    matched = await db.match_users("Москва", "Кафе и рестораны")
    assert [row["tg_id"] for row in matched] == [204]


async def test_match_users_skips_category_substring(db: Database):
    """«Банки» не должны ловиться подстрокой из другой категории."""
    await add_performer(db, 205, categories=["Банки и страхование"])
    matched = await db.match_users("Москва", "Банки")
    assert matched == []


async def test_match_users_skips_inactive_and_blocked(db: Database):
    await add_performer(db, 206)
    await add_performer(db, 207)
    await db.set_user_status(207, "blocked")

    matched = await db.match_users("Москва", "Кафе и рестораны")
    assert [row["tg_id"] for row in matched] == [206]


async def test_match_users_skips_user_without_consent(db: Database):
    await db.touch_user(208, "x", "X")
    await db.save_profile(
        208, city="Москва", age=25, has_car=False, contact="", categories=["Кафе и рестораны"]
    )
    # Профиль есть, но согласия не было — в рассылку не попадает.
    matched = await db.match_users("Москва", "Кафе и рестораны")
    assert matched == []


# ------------------------------------------------------------- занятие слотов


async def test_claim_task_happy_path(db: Database):
    await add_performer(db, 301)
    task_id = await make_task(db)
    assignment_id, code = await db.claim_task(task_id, 301)
    assert code == "ok"
    assert assignment_id is not None
    assert await db.taken_slots(task_id) == 1


async def test_claim_twice_returns_already(db: Database):
    await add_performer(db, 302)
    task_id = await make_task(db)
    first, _ = await db.claim_task(task_id, 302)
    second, code = await db.claim_task(task_id, 302)
    assert code == "already"
    assert second == first
    assert await db.taken_slots(task_id) == 1


async def test_claim_respects_slot_limit(db: Database):
    await add_performer(db, 303)
    await add_performer(db, 304)
    task_id = await make_task(db, slots=1)

    _, first = await db.claim_task(task_id, 303)
    _, second = await db.claim_task(task_id, 304)
    assert first == "ok"
    assert second == "no_slots"


async def test_claim_closed_task(db: Database):
    await add_performer(db, 305)
    task_id = await make_task(db)
    await db.set_task_status(task_id, "closed")
    _, code = await db.claim_task(task_id, 305)
    assert code == "closed"


async def test_claim_missing_task(db: Database):
    await add_performer(db, 306)
    _, code = await db.claim_task(9999, 306)
    assert code == "no_task"


async def test_cancelled_assignment_frees_slot_and_can_be_retaken(db: Database):
    await add_performer(db, 307)
    task_id = await make_task(db, slots=1)
    assignment_id, _ = await db.claim_task(task_id, 307)

    await db.set_assignment_status(assignment_id, CANCELLED)
    assert await db.taken_slots(task_id) == 0

    retaken, code = await db.claim_task(task_id, 307)
    assert code == "ok"
    assert retaken == assignment_id


async def test_concurrent_claims_do_not_oversell(db: Database):
    """Последний слот должен достаться ровно одному исполнителю."""
    for tg_id in range(400, 410):
        await add_performer(db, tg_id)
    task_id = await make_task(db, slots=3)

    results = await asyncio.gather(
        *(db.claim_task(task_id, tg_id) for tg_id in range(400, 410))
    )
    codes = [code for _, code in results]

    assert codes.count("ok") == 3
    assert codes.count("no_slots") == 7
    assert await db.taken_slots(task_id) == 3


# ------------------------------------------------------- отчёты и статусы


async def test_report_lifecycle(db: Database):
    await add_performer(db, 501)
    task_id = await make_task(db)
    assignment_id, _ = await db.claim_task(task_id, 501)

    await db.add_report_item(assignment_id, "photo", file_id="file1", position=0)
    await db.add_report_item(assignment_id, "answer", text="Сколько ждали?", position=1)
    await db.add_report_item(assignment_id, "reply", text="5 минут", position=2)
    await db.set_assignment_status(assignment_id, SUBMITTED)

    pending = await db.list_assignments_by_status(SUBMITTED)
    assert [row["id"] for row in pending] == [assignment_id]

    items = await db.list_report_items(assignment_id)
    assert [item["kind"] for item in items] == ["photo", "answer", "reply"]

    await db.set_assignment_status(assignment_id, REWORK, note="Нужно фото чека")
    row = await db.get_assignment(assignment_id)
    assert row["status"] == REWORK
    assert row["note"] == "Нужно фото чека"
    assert row["decided_at"] is not None

    # Повторная сдача заменяет прошлый черновик целиком.
    await db.clear_report(assignment_id)
    assert await db.list_report_items(assignment_id) == []

    await db.set_assignment_status(assignment_id, ACCEPTED, note="")
    await db.set_assignment_status(assignment_id, PAID)
    row = await db.get_assignment(assignment_id)
    assert row["status"] == PAID
    assert row["paid_at"] is not None


async def test_assignment_keeps_slot_through_statuses(db: Database):
    await add_performer(db, 502)
    task_id = await make_task(db, slots=1)
    assignment_id, _ = await db.claim_task(task_id, 502)

    for status in (SUBMITTED, REWORK, ACCEPTED, PAID):
        await db.set_assignment_status(assignment_id, status)
        assert await db.taken_slots(task_id) == 1, f"слот освободился на статусе {status}"


async def test_user_assignments_filtered_by_status(db: Database):
    await add_performer(db, 503)
    first = await make_task(db)
    second = await make_task(db)
    a1, _ = await db.claim_task(first, 503)
    a2, _ = await db.claim_task(second, 503)
    await db.set_assignment_status(a2, PAID)

    in_work = await db.list_user_assignments(503, (CLAIMED,))
    assert [row["id"] for row in in_work] == [a1]

    everything = await db.list_user_assignments(503, (CLAIMED, PAID))
    assert {row["id"] for row in everything} == {a1, a2}


async def test_stats(db: Database):
    await add_performer(db, 601)
    await db.touch_user(602, "new", "Новый")
    task_id = await make_task(db)
    assignment_id, _ = await db.claim_task(task_id, 601)
    await db.set_assignment_status(assignment_id, SUBMITTED)

    stats = await db.stats()
    assert stats["users_active"] == 1
    assert stats["users_total"] == 2
    assert stats["tasks_open"] == 1
    assert stats["reports_pending"] == 1
    assert stats["awaiting_payout"] == 0
