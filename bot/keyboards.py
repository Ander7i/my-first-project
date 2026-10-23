"""Инлайн-клавиатуры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import texts
from .callbacks import AssignCB, ConsentCB, ReportCB, StopCB, SurveyCB, TaskCB


def consent() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Согласен, продолжить", callback_data=ConsentCB(agree=True))
    kb.button(text="❌ Отказаться", callback_data=ConsentCB(agree=False))
    kb.adjust(1)
    return kb.as_markup()


def car() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚗 Есть", callback_data=SurveyCB(action="car", value="1"))
    kb.button(text="🚶 Нет", callback_data=SurveyCB(action="car", value="0"))
    kb.adjust(2)
    return kb.as_markup()


def skip_contact() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=SurveyCB(action="skip_contact"))
    return kb.as_markup()


def categories(all_categories: tuple[str, ...], chosen: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, name in enumerate(all_categories):
        mark = "✅" if name in chosen else "☐"
        kb.button(text=f"{mark} {name}", callback_data=SurveyCB(action="cat", value=str(index)))
    kb.button(text="➡️ Готово", callback_data=SurveyCB(action="cat_done"))
    kb.adjust(1)
    return kb.as_markup()


def claim_task(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✋ Взять задание", callback_data=TaskCB(action="claim", task_id=task_id))
    return kb.as_markup()


def my_tasks(rows: list) -> InlineKeyboardMarkup:
    """Кнопка «Сдать отчёт» для каждого задания в работе или на доработке."""
    kb = InlineKeyboardBuilder()
    for row in rows:
        if row["status"] in ("claimed", "rework"):
            kb.button(
                text=f"📨 Сдать отчёт: {row['title'][:28]}",
                callback_data=AssignCB(action="report", assignment_id=row["id"]),
            )
    kb.adjust(1)
    return kb.as_markup()


def photos_done() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➡️ Фото загружены", callback_data=ReportCB(action="photos_done"))
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


def new_task_categories(all_categories: tuple[str, ...]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for index, name in enumerate(all_categories):
        kb.button(text=name, callback_data=SurveyCB(action="newtask_cat", value=str(index)))
    kb.adjust(1)
    return kb.as_markup()


def confirm_new_task() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Создать и разослать", callback_data=TaskCB(action="create_send", task_id=0))
    kb.button(text="💾 Только создать", callback_data=TaskCB(action="create_only", task_id=0))
    kb.button(text="✖️ Отменить", callback_data=TaskCB(action="create_cancel", task_id=0))
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
