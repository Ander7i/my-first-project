"""Тесты слоя базы: подбор исполнителей, занятие слотов, отчёты."""

from __future__ import annotations

import asyncio

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
    products: list[str] | None = None,
    has_ip: bool = False,
) -> None:
    await db.touch_user(tg_id, f"user{tg_id}", f"User {tg_id}")
    await db.set_consent(tg_id)
    await db.save_profile(
        tg_id,
        city=city,
        age=30,
        has_car=False,
        has_ip=has_ip,
        contact="",
        products=products or ["debit"],
    )


async def make_task(
    db: Database,
    *,
    check_code: str = "debit_courier",
    slots: int = 1,
    city: str = "Москва",
    remote: bool = False,
) -> int:
    return await db.create_task(
        {
            "check_code": check_code,
            "bank": "Тестбанк",
            "title": "Доставка карты — Тестбанк",
            "city": city,
            "remote": remote,
            "payment": 800,
            "deadline": "до 30 октября",
            "slots": slots,
            "notes": "",
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
    await add_performer(db, 101, has_ip=True)
    row = await db.get_user(101)
    assert row["status"] == "active"
    assert row["consent_at"] is not None
    assert row["city"] == "Москва"
    assert row["has_ip"] == 1


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


async def test_set_has_ip(db: Database):
    await add_performer(db, 103, has_ip=False)
    await db.set_has_ip(103, True)
    assert (await db.get_user(103))["has_ip"] == 1


# ------------------------------------------------------------------- подбор


async def test_match_users_by_product_and_city(db: Database):
    await add_performer(db, 201, city="Москва", products=["debit"])
    await add_performer(db, 202, city="Казань", products=["debit"])
    await add_performer(db, 203, city="Москва", products=["credit"])

    matched = await db.match_users("debit", city="Москва")
    assert [row["tg_id"] for row in matched] == [201]


async def test_match_users_ignores_case_and_spaces(db: Database):
    await add_performer(db, 204, city="  москва ")
    matched = await db.match_users("debit", city="Москва")
    assert [row["tg_id"] for row in matched] == [204]


async def test_remote_task_ignores_city(db: Database):
    """Проверку поддержки можно делать из любого города."""
    await add_performer(db, 205, city="Москва", products=["service"])
    await add_performer(db, 206, city="Владивосток", products=["service"])

    matched = await db.match_users("service", city="", remote=True)
    assert {row["tg_id"] for row in matched} == {205, 206}


async def test_match_users_skips_product_substring(db: Database):
    """Код продукта не должен ловиться подстрокой соседнего."""
    await add_performer(db, 207, products=["debit_extra"])
    assert await db.match_users("debit", city="Москва") == []


async def test_match_users_skips_inactive_and_blocked(db: Database):
    await add_performer(db, 208)
    await add_performer(db, 209)
    await db.set_user_status(209, "blocked")

    matched = await db.match_users("debit", city="Москва")
    assert [row["tg_id"] for row in matched] == [208]


async def test_match_users_skips_user_without_consent(db: Database):
    await db.touch_user(210, "x", "X")
    await db.save_profile(
        210,
        city="Москва",
        age=25,
        has_car=False,
        has_ip=False,
        contact="",
        products=["debit"],
    )
    # Профиль есть, но согласия не было — в рассылку не попадает.
    assert await db.match_users("debit", city="Москва") == []


async def test_match_requires_existing_ip(db: Database):
    """Расчётный счёт может проверять только действующий ИП."""
    await add_performer(db, 211, products=["settlement"], has_ip=True)
    await add_performer(db, 212, products=["settlement"], has_ip=False)

    matched = await db.match_users("settlement", city="Москва", requires_ip=True)
    assert [row["tg_id"] for row in matched] == [211]


async def test_match_requires_absent_ip(db: Database):
    """Задание на регистрацию ИП не уходит тем, у кого ИП уже есть."""
    await add_performer(db, 213, products=["ip"], has_ip=True)
    await add_performer(db, 214, products=["ip"], has_ip=False)

    matched = await db.match_users("ip", city="Москва", requires_ip=False)
    assert [row["tg_id"] for row in matched] == [214]


async def test_match_ignores_ip_when_not_required(db: Database):
    await add_performer(db, 215, products=["debit"], has_ip=True)
    await add_performer(db, 216, products=["debit"], has_ip=False)

    matched = await db.match_users("debit", city="Москва")
    assert {row["tg_id"] for row in matched} == {215, 216}


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


async def test_set_ack_records_timestamp(db: Database):
    await add_performer(db, 320, products=["ip"])
    task_id = await make_task(db, check_code="ip_registration", remote=True)
    assignment_id, _ = await db.claim_task(task_id, 320)

    assert (await db.get_assignment(assignment_id))["ack_at"] is None
    await db.set_ack(assignment_id)
    assert (await db.get_assignment(assignment_id))["ack_at"] is not None


# ------------------------------------------------- подходящие задания в боте


async def test_open_tasks_for_user_filters_by_product(db: Database):
    await add_performer(db, 501, products=["debit"])
    good = await make_task(db, check_code="debit_courier")
    await make_task(db, check_code="credit_courier")

    user = await db.get_user(501)
    rows = await db.open_tasks_for_user(user)
    assert [row["id"] for row in rows] == [good]


async def test_open_tasks_for_user_filters_by_ip(db: Database):
    await add_performer(db, 502, products=["settlement", "ip"], has_ip=False)
    await make_task(db, check_code="settlement_open", remote=True)
    ip_task = await make_task(db, check_code="ip_registration", remote=True)

    user = await db.get_user(502)
    rows = await db.open_tasks_for_user(user)
    # Без ИП доступна только регистрация, не открытие счёта.
    assert [row["id"] for row in rows] == [ip_task]


async def test_open_tasks_for_user_hides_remote_city_mismatch(db: Database):
    await add_performer(db, 503, city="Казань", products=["debit", "service"])
    await make_task(db, check_code="debit_courier", city="Москва")
    remote = await make_task(db, check_code="support_phone", remote=True)

    user = await db.get_user(503)
    rows = await db.open_tasks_for_user(user)
    assert [row["id"] for row in rows] == [remote]


async def test_open_tasks_for_user_hides_taken_and_full(db: Database):
    await add_performer(db, 504, products=["debit"])
    await add_performer(db, 505, products=["debit"])
    mine = await make_task(db, check_code="debit_courier")
    full = await make_task(db, check_code="debit_courier", slots=1)

    await db.claim_task(mine, 504)
    await db.claim_task(full, 505)

    user = await db.get_user(504)
    rows = await db.open_tasks_for_user(user)
    # Своё уже взято, чужое занято целиком — показывать нечего.
    assert rows == []


async def test_dropped_task_returns_to_available(db: Database):
    """Отказ должен возвращать задание в список, иначе слот залипает."""
    await add_performer(db, 507, products=["debit"])
    task_id = await make_task(db, slots=1)
    assignment_id, _ = await db.claim_task(task_id, 507)

    user = await db.get_user(507)
    assert await db.open_tasks_for_user(user) == []

    await db.set_assignment_status(assignment_id, CANCELLED)

    rows = await db.open_tasks_for_user(user)
    assert [row["id"] for row in rows] == [task_id]


async def test_open_tasks_for_user_hides_closed(db: Database):
    await add_performer(db, 506, products=["debit"])
    task_id = await make_task(db)
    await db.set_task_status(task_id, "closed")

    user = await db.get_user(506)
    assert await db.open_tasks_for_user(user) == []


# ------------------------------------------------------- отчёты и статусы


async def test_report_lifecycle(db: Database):
    await add_performer(db, 601)
    task_id = await make_task(db)
    assignment_id, _ = await db.claim_task(task_id, 601)

    await db.add_report_item(assignment_id, "photo", file_id="file1", position=0)
    await db.add_report_item(assignment_id, "answer", text="Во сколько приехал?", position=1)
    await db.add_report_item(assignment_id, "reply", text="В 14:05", position=2)
    await db.set_assignment_status(assignment_id, SUBMITTED)

    pending = await db.list_assignments_by_status(SUBMITTED)
    assert [row["id"] for row in pending] == [assignment_id]

    items = await db.list_report_items(assignment_id)
    assert [item["kind"] for item in items] == ["photo", "answer", "reply"]

    await db.set_assignment_status(assignment_id, REWORK, note="Нужно фото документов")
    row = await db.get_assignment(assignment_id)
    assert row["status"] == REWORK
    assert row["note"] == "Нужно фото документов"
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
    await add_performer(db, 602)
    task_id = await make_task(db, slots=1)
    assignment_id, _ = await db.claim_task(task_id, 602)

    for status in (SUBMITTED, REWORK, ACCEPTED, PAID):
        await db.set_assignment_status(assignment_id, status)
        assert await db.taken_slots(task_id) == 1, f"слот освободился на статусе {status}"


async def test_user_assignments_filtered_by_status(db: Database):
    await add_performer(db, 603)
    first = await make_task(db)
    second = await make_task(db)
    a1, _ = await db.claim_task(first, 603)
    a2, _ = await db.claim_task(second, 603)
    await db.set_assignment_status(a2, PAID)

    in_work = await db.list_user_assignments(603, (CLAIMED,))
    assert [row["id"] for row in in_work] == [a1]

    everything = await db.list_user_assignments(603, (CLAIMED, PAID))
    assert {row["id"] for row in everything} == {a1, a2}


async def test_stats(db: Database):
    await add_performer(db, 701, has_ip=True)
    await db.touch_user(702, "new", "Новый")
    task_id = await make_task(db)
    assignment_id, _ = await db.claim_task(task_id, 701)
    await db.set_assignment_status(assignment_id, SUBMITTED)

    stats = await db.stats()
    assert stats["users_active"] == 1
    assert stats["users_total"] == 2
    assert stats["users_with_ip"] == 1
    assert stats["tasks_open"] == 1
    assert stats["reports_pending"] == 1
    assert stats["awaiting_payout"] == 0


# ------------------------------------------------------------------ миграции


async def test_migration_adds_columns_to_old_db(tmp_path):
    """Старая база без новых колонок должна открыться и дополниться."""
    import aiosqlite

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            """
            CREATE TABLE users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                city TEXT,
                age INTEGER,
                has_car INTEGER NOT NULL DEFAULT 0,
                contact TEXT,
                consent_at TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            "INSERT INTO users (tg_id, full_name, status, created_at) "
            "VALUES (1, 'Старый', 'active', '2026-01-01')"
        )
        await conn.commit()

    database = Database(path)
    await database.connect()
    try:
        row = await database.get_user(1)
        # Анкета сохранилась, новые поля получили значения по умолчанию.
        assert row["full_name"] == "Старый"
        assert row["has_ip"] == 0
        assert row["products"] == ""
    finally:
        await database.close()
