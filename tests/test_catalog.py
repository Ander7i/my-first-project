"""Тесты каталога: целостность, пригодность заданий, отрисовка карточек."""

from __future__ import annotations

import pytest_asyncio

from bot import catalog
from bot.db import Database
from bot.utils import render_check, unsuitable_reason


def test_catalog_is_valid():
    """Опечатка в коде продукта должна ловиться при старте, а не у человека."""
    catalog.validate()


def test_every_check_has_steps_and_requirements():
    for check in catalog.CHECKS:
        assert check.steps, f"{check.code}: нет шагов"
        assert check.requirements, f"{check.code}: нет требований"
        assert check.duration, f"{check.code}: не указана длительность"
        assert check.summary, f"{check.code}: нет описания"


def test_money_touching_checks_warn_about_handover():
    """Там, где оформляется продукт, должно быть сказано: он остаётся у вас.

    Это главный признак, отличающий проверку от дропперской схемы, поэтому
    предупреждение обязательно и его отсутствие — ошибка каталога.
    """
    for check in catalog.CHECKS:
        if check.product not in {"debit", "credit", "settlement", "ip"}:
            continue
        joined = " ".join(check.warnings).lower()
        assert "остаёт" in joined or "остают" in joined, (
            f"{check.code}: не сказано, что продукт остаётся у исполнителя"
        )
        assert "не передав" in joined or "не передают" in joined or (
            "никому" in joined
        ), f"{check.code}: не сказано, что доступ никому не передаётся"


def test_ip_registration_discloses_contributions():
    check = catalog.check_by_code("ip_registration")
    assert check is not None
    assert check.needs_ack
    assert check.requires_ip is False

    joined = " ".join(check.warnings) + check.ack_text
    # Человек должен увидеть сумму, нулевой доход и порядок закрытия.
    assert catalog.IP_FIXED_FEE in joined
    assert "нулев" in joined.lower() or "без дохода" in joined.lower()
    assert "закры" in joined.lower()
    assert "деклараци" in joined.lower()


def test_credit_checks_warn_about_credit_history():
    check = catalog.check_by_code("credit_online")
    assert check is not None
    joined = " ".join(check.warnings).lower()
    assert "кредитной истории" in joined


def test_settlement_requires_ip_and_registration_forbids_it():
    assert catalog.check_by_code("settlement_open").requires_ip is True
    assert catalog.check_by_code("ip_registration").requires_ip is False


def test_remote_checks_do_not_need_presence():
    remote_codes = {c.code for c in catalog.CHECKS if c.remote}
    # Телефон и чат делаются откуда угодно, курьер и отделение — нет.
    assert {"support_phone", "support_chat"} <= remote_codes
    assert "debit_courier" not in remote_codes
    assert "branch_visit" not in remote_codes


def test_checks_for_product_covers_all():
    total = sum(len(catalog.checks_for_product(p.code)) for p in catalog.PRODUCTS)
    assert total == len(catalog.CHECKS)


def test_unknown_codes_return_none():
    assert catalog.check_by_code("нет_такого") is None
    assert catalog.product_by_code("нет_такого") is None


def test_render_check_escapes_and_includes_sections():
    check = catalog.check_by_code("debit_courier")
    card = render_check(check)
    assert check.title in card
    assert "Что делать по шагам" in card
    assert "Что нужно иметь" in card
    assert "Знайте заранее" in card
    assert "Чек-лист отчёта" in card
    assert "Приложить к отчёту" in card


def test_render_check_marks_ack_requirement():
    card = render_check(catalog.check_by_code("ip_registration"))
    assert "отдельное предупреждение" in card


def test_render_check_shows_remote_flag():
    assert "Дистанционно" in render_check(catalog.check_by_code("support_phone"))
    assert "присутствие" in render_check(catalog.check_by_code("branch_visit"))


# ------------------------------------------------- пригодность задания человеку


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "suit.db")
    await database.connect()
    yield database
    await database.close()


async def _user(db: Database, **kwargs):
    defaults = dict(city="Москва", products=["debit"], has_ip=False)
    defaults.update(kwargs)
    await db.touch_user(1, "u", "U")
    await db.set_consent(1)
    await db.save_profile(
        1,
        city=defaults["city"],
        age=30,
        has_car=False,
        has_ip=defaults["has_ip"],
        contact="",
        products=defaults["products"],
    )
    return await db.get_user(1)


async def _task(db: Database, **kwargs):
    defaults = dict(check_code="debit_courier", city="Москва", remote=False)
    defaults.update(kwargs)
    task_id = await db.create_task(
        {
            "check_code": defaults["check_code"],
            "bank": "Банк",
            "title": "T",
            "city": defaults["city"],
            "remote": defaults["remote"],
            "payment": 500,
            "deadline": "скоро",
            "slots": 1,
            "notes": "",
        },
        created_by=1,
    )
    return await db.get_task(task_id)


async def test_suitable_task_has_no_reason(db: Database):
    user = await _user(db)
    task = await _task(db)
    assert unsuitable_reason(user, task) is None


async def test_wrong_product_is_rejected(db: Database):
    user = await _user(db, products=["service"])
    task = await _task(db)
    assert unsuitable_reason(user, task) is not None


async def test_wrong_city_is_rejected(db: Database):
    user = await _user(db, city="Казань")
    task = await _task(db, city="Москва")
    assert "город" in unsuitable_reason(user, task)


async def test_remote_task_accepts_any_city(db: Database):
    user = await _user(db, city="Казань", products=["service"])
    task = await _task(db, check_code="support_phone", remote=True, city="")
    assert unsuitable_reason(user, task) is None


async def test_settlement_rejected_without_ip(db: Database):
    user = await _user(db, products=["settlement"], has_ip=False)
    task = await _task(db, check_code="settlement_open", remote=True)
    assert "ИП" in unsuitable_reason(user, task)


async def test_ip_registration_rejected_with_existing_ip(db: Database):
    user = await _user(db, products=["ip"], has_ip=True)
    task = await _task(db, check_code="ip_registration", remote=True)
    assert "ИП" in unsuitable_reason(user, task)


async def test_unknown_check_is_rejected(db: Database):
    user = await _user(db)
    task = await _task(db, check_code="исчез_из_каталога")
    assert unsuitable_reason(user, task) is not None
