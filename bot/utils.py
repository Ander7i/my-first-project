"""Вспомогательные функции: карточки, рассылка, пригодность заданий."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

from . import texts
from .catalog import CheckType, check_by_code, product_by_code
from .db import Database, normalize_city

log = logging.getLogger(__name__)

# Пауза между сообщениями при рассылке: Telegram ограничивает ~30 сообщений в секунду.
BROADCAST_DELAY = 0.05


def bullets(items: Sequence[str]) -> str:
    return "\n".join(f"• {escape(item)}" for item in items) if items else "• —"


def numbered(items: Sequence[str]) -> str:
    if not items:
        return "—"
    return "\n".join(f"{i}. {escape(item)}" for i, item in enumerate(items, 1))


def performer_name(row: Any) -> str:
    """Имя исполнителя для админских карточек."""
    keys = row.keys()
    name = escape(row["full_name"] or "без имени")
    username = row["username"] if "username" in keys else None
    if username:
        return f"{name} (@{escape(username)})"
    user_id = row["user_id"] if "user_id" in keys else row["tg_id"]
    return f"{name} (id <code>{user_id}</code>)"


def profile_card(row: Any) -> str:
    """Анкета исполнителя — её видит и сам исполнитель, и администратор."""
    codes = [c for c in (row["products"] or "").split(",") if c]
    titles = []
    for code in codes:
        product = product_by_code(code)
        titles.append(product.title if product else code)
    lines = [
        f"📍 Город: <b>{escape(row['city'] or '—')}</b>",
        f"🎂 Возраст: <b>{row['age'] or '—'}</b>",
        f"🚗 Авто: <b>{'есть' if row['has_car'] else 'нет'}</b>",
        f"🧾 Действующее ИП: <b>{'есть' if row['has_ip'] else 'нет'}</b>",
        f"🏦 Продукты: {escape(', '.join(titles)) if titles else 'не выбраны'}",
    ]
    if row["contact"]:
        lines.append(f"📞 Связь: {escape(row['contact'])}")
    return "\n".join(lines) + "\n"


def task_title(check: CheckType, bank: str) -> str:
    return f"{check.title} — {bank}" if bank else check.title


def task_location(task: Any) -> str:
    if task["remote"]:
        return texts.CHECK_REMOTE
    return f"📍 {escape(task['city'] or '—')}"


def task_card(task: Any, free_slots: int | None = None) -> str:
    free = task["slots"] if free_slots is None else free_slots
    return texts.TASK_CARD.format(
        title=escape(task["title"]),
        bank=escape(task["bank"] or "—"),
        location=task_location(task),
        payment=task["payment"],
        deadline=escape(task["deadline"]),
        free=free,
        slots=task["slots"],
    )


def render_check(check: CheckType) -> str:
    """Полная карточка вида проверки для каталога."""
    product = product_by_code(check.product)
    location = texts.CHECK_REMOTE if check.remote else texts.CHECK_ONSITE
    if check.requires_ip is True:
        location += "\n" + texts.CHECK_NEEDS_IP
    elif check.requires_ip is False:
        location += "\n" + texts.CHECK_NEEDS_NO_IP

    warnings = (
        texts.CHECK_WARNINGS_BLOCK.format(items=bullets(check.warnings))
        if check.warnings
        else ""
    )
    attachments = (
        texts.CHECK_ATTACHMENTS_BLOCK.format(items=bullets(check.attachments))
        if check.attachments
        else ""
    )

    card = texts.CHECK_CARD.format(
        title=escape(check.title),
        product=escape(product.title if product else check.product),
        duration=escape(check.duration),
        location=location,
        summary=escape(check.summary),
        steps=numbered(check.steps),
        requirements=bullets(check.requirements),
        warnings=warnings,
        checklist=numbered(check.checklist),
        attachments=attachments,
    )
    if check.needs_ack:
        card += texts.CHECK_NEEDS_ACK
    return card


def unsuitable_reason(
    user: Any,
    task: Any,
    *,
    prior_count: int = 0,
    days_since_last: int | None = None,
) -> str | None:
    """Почему задание не подходит исполнителю. None — подходит.

    Проверяется при взятии задания, а не только при рассылке: ссылку на
    задание могут переслать, да и анкета человека могла поменяться.

    prior_count и days_since_last — история по этому же виду проверки
    (db.check_history). Без них лимиты повторов не проверяются.
    """
    check = check_by_code(task["check_code"])
    if check is None:
        return texts.TASK_GONE

    products = {p for p in (user["products"] or "").split(",") if p}
    if check.product not in products:
        return texts.REASON_PRODUCT

    if check.requires_ip is True and not user["has_ip"]:
        return texts.REASON_NEEDS_IP
    if check.requires_ip is False and user["has_ip"]:
        return texts.REASON_NEEDS_NO_IP

    if not task["remote"] and task["city_key"] != normalize_city(user["city"] or ""):
        return texts.REASON_CITY

    # Лимиты повторов: защищают человека от накопления записей в кредитной
    # истории и от упора в законный лимит на число одновременных займов.
    if check.max_per_user and prior_count >= check.max_per_user:
        return texts.REASON_MAX_REACHED.format(limit=check.max_per_user)

    if check.cooldown_days and days_since_last is not None:
        left = check.cooldown_days - days_since_last
        if left > 0:
            return texts.REASON_COOLDOWN.format(days=left)

    return None


def format_answers(items: Sequence[Any]) -> str:
    """Собирает ответы из отчёта в читаемый блок."""
    lines = []
    number = 0
    for item in items:
        if item["kind"] == "answer":
            number += 1
            question = escape(item["text"] or "")
            lines.append(f"<b>{number}. {question}</b>")
        elif item["kind"] == "reply":
            lines.append(escape(item["text"] or ""))
        elif item["kind"] == "comment":
            lines.append(f"\n💬 <i>{escape(item['text'] or '')}</i>")
    return "\n".join(lines) if lines else "— без ответов"


async def send_safe(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    db: Database | None = None,
) -> bool:
    """Отправляет сообщение, не падая на заблокировавших бота.

    Если пользователь заблокировал бота, помечаем его в базе как blocked,
    чтобы не слать ему задания впустую.
    """
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return True
    except TelegramRetryAfter as exc:
        # Поймали лимит — ждём столько, сколько просит Telegram, и пробуем ещё раз.
        log.warning("Лимит Telegram, ждём %s с", exc.retry_after)
        await asyncio.sleep(exc.retry_after)
        try:
            await bot.send_message(chat_id, text, reply_markup=reply_markup)
            return True
        except Exception as retry_exc:
            log.warning("Повтор не удался для %s: %s", chat_id, retry_exc)
            return False
    except TelegramForbiddenError:
        log.info("Пользователь %s заблокировал бота", chat_id)
        if db is not None:
            await db.set_user_status(chat_id, "blocked")
        return False
    except TelegramBadRequest as exc:
        log.warning("Не доставлено %s: %s", chat_id, exc)
        return False


async def broadcast(
    bot: Bot,
    db: Database,
    chat_ids: Iterable[int],
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> tuple[int, int]:
    """Рассылает текст списку чатов. Возвращает (доставлено, не доставлено)."""
    sent = failed = 0
    for chat_id in chat_ids:
        ok = await send_safe(bot, chat_id, text, reply_markup=reply_markup, db=db)
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)
    return sent, failed


async def notify_admins(
    bot: Bot,
    admin_ids: Iterable[int],
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as exc:
            log.warning("Не смог уведомить админа %s: %s", admin_id, exc)
