"""Тесты шаблонов сообщений.

Несовпадение подстановок в тексте даёт KeyError прямо в обработчике, то есть
у живого человека посреди диалога. Эти тесты рендерят каждый шаблон теми же
аргументами, что передают обработчики.
"""

from __future__ import annotations

import pytest_asyncio

from bot import texts
from bot.catalog import CHECKS, check_by_code
from bot.db import Database
from bot.handlers.admin import _preview_row
from bot.utils import (
    bullets,
    numbered,
    performer_name,
    profile_card,
    render_check,
    task_card,
    task_location,
    task_title,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "tpl.db")
    await database.connect()
    yield database
    await database.close()


async def _seed(db: Database, check_code: str = "debit_courier"):
    await db.touch_user(1, "vasya", "Вася <Тест> & Ко")
    await db.set_consent(1)
    await db.save_profile(
        1,
        city="Москва",
        age=30,
        has_car=True,
        has_ip=True,
        contact="+7 900 000-00-00",
        products=["debit", "settlement"],
    )
    task_id = await db.create_task(
        {
            "check_code": check_code,
            "bank": "Банк <Тест> & Ко",
            "title": "Проверка — Банк",
            "city": "Москва",
            "remote": False,
            "payment": 1500,
            "deadline": "до 30 октября",
            "slots": 2,
            "notes": "Тариф <Премиум> & доставка",
        },
        created_by=1,
    )
    assignment_id, _ = await db.claim_task(task_id, 1)
    return task_id, assignment_id


def test_render_check_for_every_catalog_entry():
    """Любая карточка каталога должна рендериться без ошибок подстановки."""
    for check in CHECKS:
        card = render_check(check)
        assert check.title in card
        # Незакрытая подстановка осталась бы фигурной скобкой в тексте.
        assert "{" not in card and "}" not in card


async def test_task_card_and_profile_card(db: Database):
    await _seed(db)
    task = await db.get_task(1)
    user = await db.get_user(1)

    card = task_card(task, free_slots=1)
    assert "Мест: 1 из 2" in card
    assert "&lt;Тест&gt; &amp; Ко" in card
    assert "{" not in card

    profile = profile_card(user)
    assert "Москва" in profile
    assert "Действующее ИП: <b>есть</b>" in profile
    # Коды продуктов показываются человеку названиями, а не кодами.
    assert "Дебетовая карта" in profile
    assert "debit" not in profile


async def test_survey_and_registered_templates(db: Database):
    await _seed(db)
    user = await db.get_user(1)
    for template in (texts.SURVEY_DONE, texts.ALREADY_REGISTERED):
        rendered = template.format(profile=profile_card(user))
        assert "{" not in rendered


async def test_task_claimed_template(db: Database):
    task_id, _ = await _seed(db)
    task = await db.get_task(task_id)
    check = check_by_code(task["check_code"])

    rendered = texts.TASK_CLAIMED.format(
        title=task["title"],
        bank=task["bank"],
        deadline=task["deadline"],
        payment=task["payment"],
        steps=numbered(check.steps),
        notes=texts.TASK_NOTES.format(notes=task["notes"]),
        checklist=numbered(check.checklist),
        attachments=texts.CHECK_ATTACHMENTS_BLOCK.format(items=bullets(check.attachments)),
    )
    assert "{" not in rendered
    assert check.steps[0] in rendered


async def test_report_start_template(db: Database):
    _, assignment_id = await _seed(db)
    assignment = await db.get_assignment(assignment_id)
    check = check_by_code(assignment["check_code"])

    rendered = texts.REPORT_START.format(
        title=assignment["title"], attachments=bullets(check.attachments)
    )
    assert "{" not in rendered


async def test_admin_report_template(db: Database):
    _, assignment_id = await _seed(db)
    await db.add_report_item(assignment_id, "answer", text="Во сколько приехал?", position=0)
    await db.add_report_item(assignment_id, "reply", text="В 14:05", position=1)
    await db.add_report_item(assignment_id, "comment", text="Был вежлив", position=2)

    assignment = await db.get_assignment(assignment_id)
    items = await db.list_report_items(assignment_id)

    from bot.utils import format_answers

    rendered = texts.ADMIN_NEW_REPORT.format(
        title=assignment["title"],
        bank=assignment["bank"],
        performer=performer_name(assignment),
        location=task_location(assignment),
        payment=assignment["payment"],
        answers=format_answers(items),
    )
    assert "{" not in rendered
    assert "Во сколько приехал?" in rendered
    assert "@vasya" in rendered


async def test_admin_menu_matches_stats_keys(db: Database):
    """ADMIN_MENU подставляется через **stats — ключи должны совпадать."""
    stats = await db.stats()
    rendered = texts.ADMIN_MENU.format(**stats)
    assert "{" not in rendered


async def test_new_task_preview_template(db: Database):
    data = {
        "check_code": "debit_courier",
        "bank": "Банк",
        "title": task_title(check_by_code("debit_courier"), "Банк"),
        "city": "Москва",
        "remote": False,
        "payment": 1200,
        "deadline": "до пятницы",
        "slots": 3,
        "notes": "Детали",
    }
    rendered = texts.NEW_TASK_PREVIEW.format(
        card=task_card(_preview_row(data)),
        notes=texts.TASK_NOTES.format(notes=data["notes"]),
        matches=0,
    )
    assert "{" not in rendered
    assert "Мест: 3 из 3" in rendered


async def test_group_announce_template(db: Database):
    task_id, _ = await _seed(db)
    task = await db.get_task(task_id)
    rendered = texts.GROUP_TASK_ANNOUNCE.format(
        bank=task["bank"],
        location=task_location(task),
        payment=task["payment"],
        deadline=task["deadline"],
        title=task["title"],
    )
    assert "{" not in rendered


async def test_remote_task_location_label(db: Database):
    task_id = await db.create_task(
        {
            "check_code": "support_phone",
            "bank": "Банк",
            "title": "Поддержка",
            "city": "",
            "remote": True,
            "payment": 400,
            "deadline": "до среды",
            "slots": 1,
            "notes": "",
        },
        created_by=1,
    )
    task = await db.get_task(task_id)
    assert "Дистанционно" in task_location(task)
    assert "{" not in task_card(task)


def test_user_notification_templates():
    for template in (
        texts.REPORT_ACCEPTED_USER,
        texts.REPORT_PAID_USER,
    ):
        assert "{" not in template.format(title="Задание", payment=1000)
    assert "{" not in texts.REPORT_REWORK_USER.format(title="З", note="Нужно фото")
    assert "{" not in texts.TASK_NOT_FOR_YOU.format(reason=texts.REASON_NEEDS_IP)
    assert "{" not in texts.TASK_PUBLISHED.format(task_id=1, sent=2, failed=0)
    assert "{" not in texts.ANNOUNCE_DONE.format(sent=1, failed=0)
    assert "{" not in texts.POST_FAILED.format(error="нет прав")
    assert "{" not in texts.TASK_NO_MATCHES.format(task_id=1, filters="продукт «X»")
    assert "{" not in texts.REPORT_QUESTION.format(step=1, total=3, question="Q")
    assert "{" not in texts.REPORT_PHOTO_OK.format(count=2)
    assert "{" not in texts.REPORT_PREVIEW.format(title="T", photos=2, answers="A")
    assert "{" not in texts.GROUP_WELCOME.format(name="Вася")
    assert "{" not in texts.PRODUCT_CARD.format(title="T", summary="S")
