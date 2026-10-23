"""Инлайн-клавиатуры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import texts
from .callbacks import (
    AckCB,
    AssignCB,
    CatalogCB,
    ConsentCB,
    NewTaskCB,
    ReportCB,
    StopCB,
    SurveyCB,
    TaskCB,
)
from .catalog import CheckType, Product


def consent() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Согласен, продолжить", callback_data=ConsentCB(agree=True))
    kb.button(text="❌ Отказаться", callback_data=ConsentCB(agree=False))
    kb.adjust(1)
    return kb.as_markup()


def yes_no(action: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да", callback_data=SurveyCB(action=action, value="1"))
    kb.button(text="Нет", callback_data=SurveyCB(action=action, value="0"))
    kb.adjust(2)
    return kb.as_markup()


def skip_contact() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=SurveyCB(action="skip_contact"))
    return kb.as_markup()


def products(all_products: tuple[Product, ...], chosen: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for product in all_products:
        mark = "✅" if product.code in chosen else "☐"
        kb.button(
            text=f"{mark} {product.title}",
            callback_data=SurveyCB(action="product", value=product.code),
        )
    kb.button(text="➡️ Готово", callback_data=SurveyCB(action="products_done"))
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------------- каталог


def catalog_products(all_products: tuple[Product, ...]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for product in all_products:
        kb.button(
            text=product.title,
            callback_data=CatalogCB(action="product", code=product.code),
        )
    kb.adjust(1)
    return kb.as_markup()


def catalog_checks(checks: tuple[CheckType, ...]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for check in checks:
        kb.button(
            text=check.title,
            callback_data=CatalogCB(action="check", code=check.code),
        )
    kb.button(text="⬅️ К продуктам", callback_data=CatalogCB(action="products"))
    kb.adjust(1)
    return kb.as_markup()


def catalog_back(product_code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=CatalogCB(action="product", code=product_code))
    kb.button(text="📋 Открытые задания", callback_data=CatalogCB(action="available"))
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------------- задания


def available_tasks(rows: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.button(
            text=f"{row['payment']} ₽ · {row['title'][:34]}",
            callback_data=TaskCB(action="view", task_id=row["id"]),
        )
    kb.adjust(1)
    return kb.as_markup()


def claim_task(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✋ Взять задание", callback_data=TaskCB(action="claim", task_id=task_id))
    return kb.as_markup()


def acknowledge(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.ACK_CONFIRM_BUTTON, callback_data=AckCB(agree=True, task_id=task_id))
    kb.button(text="✖️ Не беру", callback_data=AckCB(agree=False, task_id=task_id))
    kb.adjust(1)
    return kb.as_markup()


def my_tasks(rows: list) -> InlineKeyboardMarkup:
    """Для каждого задания в работе — ссылка и сдача отчёта."""
    kb = InlineKeyboardBuilder()
    for row in rows:
        if row["status"] in ("claimed", "rework"):
            kb.button(
                text=f"🔗 Ссылка: {row['title'][:26]}",
                callback_data=AssignCB(action="link", assignment_id=row["id"]),
            )
            kb.button(
                text=f"📨 Сдать отчёт: {row['title'][:24]}",
                callback_data=AssignCB(action="report", assignment_id=row["id"]),
            )
    kb.adjust(1)
    return kb.as_markup()


# ----------------------------------------------------------------------- отчёт


def photos_done() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➡️ Файлы загружены", callback_data=ReportCB(action="photos_done"))
    kb.button(text="✖️ Отменить", callback_data=ReportCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def skip_comment() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=ReportCB(action="skip_comment"))
    kb.button(text="✖️ Отменить", callback_data=ReportCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def report_preview() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📨 Отправить отчёт", callback_data=ReportCB(action="submit"))
    kb.button(text="✖️ Отменить", callback_data=ReportCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


# -------------------------------------------------------------------- админские


def review_report(assignment_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Принять",
        callback_data=AssignCB(action="accept", assignment_id=assignment_id),
    )
    kb.button(
        text="♻️ На доработку",
        callback_data=AssignCB(action="rework", assignment_id=assignment_id),
    )
    kb.adjust(2)
    return kb.as_markup()


def mark_paid(assignment_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="💰 Отметить оплату",
        callback_data=AssignCB(action="paid", assignment_id=assignment_id),
    )
    return kb.as_markup()


def task_actions(task_id: int, status: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📤 Разослать исполнителям",
        callback_data=TaskCB(action="send", task_id=task_id),
    )
    kb.button(text="📣 В группу", callback_data=TaskCB(action="to_group", task_id=task_id))
    if status == "open":
        kb.button(text="🔒 Закрыть", callback_data=TaskCB(action="close", task_id=task_id))
    else:
        kb.button(text="🔓 Открыть", callback_data=TaskCB(action="reopen", task_id=task_id))
    kb.adjust(1)
    return kb.as_markup()


def new_task_products(all_products: tuple[Product, ...]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for product in all_products:
        kb.button(
            text=product.title,
            callback_data=NewTaskCB(action="product", code=product.code),
        )
    kb.button(text="✖️ Отменить", callback_data=NewTaskCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def new_task_checks(checks: tuple[CheckType, ...]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for check in checks:
        kb.button(
            text=check.title,
            callback_data=NewTaskCB(action="check", code=check.code),
        )
    kb.button(text="⬅️ Назад", callback_data=NewTaskCB(action="back_products"))
    kb.adjust(1)
    return kb.as_markup()


def task_link(url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BTN_OPEN_LINK, url=url)
    return kb.as_markup()


def write_manager(url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BTN_WRITE_MANAGER, url=url)
    return kb.as_markup()


def manual_link() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Выдам сам", callback_data=NewTaskCB(action="manual_link"))
    kb.button(text="✖️ Отменить", callback_data=NewTaskCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def skip_notes() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Без деталей", callback_data=NewTaskCB(action="skip_notes"))
    kb.button(text="✖️ Отменить", callback_data=NewTaskCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def confirm_new_task() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Создать и разослать", callback_data=NewTaskCB(action="create_send"))
    kb.button(text="💾 Только создать", callback_data=NewTaskCB(action="create_only"))
    kb.button(text="✖️ Отменить", callback_data=NewTaskCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def confirm_stop() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=StopCB(confirm=True))
    kb.button(text="Отмена", callback_data=StopCB(confirm=False))
    kb.adjust(1)
    return kb.as_markup()


def open_bot(bot_username: str, payload: str = "join") -> InlineKeyboardMarkup:
    """Кнопка-ссылка из группы в личку бота (бот не может написать первым)."""
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BTN_JOIN, url=f"https://t.me/{bot_username}?start={payload}")
    return kb.as_markup()
