"""Вспомогательные функции: карточки, рассылка, разбор чек-листа."""

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
from .db import Database

log = logging.getLogger(__name__)

# Пауза между сообщениями при рассылке: Telegram ограничивает ~30 сообщений в секунду.
BROADCAST_DELAY = 0.05


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
    cats = row["categories"] or ""
    cat_list = ", ".join(c for c in cats.split(",") if c) or "не выбраны"
    lines = [
        f"📍 Город: <b>{escape(row['city'] or '—')}</b>",
        f"🎂 Возраст: <b>{row['age'] or '—'}</b>",
        f"🚗 Авто: <b>{'есть' if row['has_car'] else 'нет'}</b>",
        f"🏷 Категории: {escape(cat_list)}",
    ]
    if row["contact"]:
        lines.append(f"📞 Связь: {escape(row['contact'])}")
    return "\n".join(lines) + "\n"


def task_card(task: Any, free_slots: int | None = None) -> str:
    free = task["slots"] if free_slots is None else free_slots
    return texts.TASK_CARD.format(
        title=escape(task["title"]),
        city=escape(task["city"]),
        category=escape(task["category"]),
        payment=task["payment"],
        deadline=escape(task["deadline"]),
        free=free,
        slots=task["slots"],
    )


def parse_checklist(raw: str) -> list[str]:
    """Чек-лист хранится построчно; пустые строки игнорируем."""
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def format_checklist(raw: str) -> str:
    items = parse_checklist(raw)
    if not items:
        return "— (свободная форма)"
    return "\n".join(f"{i}. {escape(item)}" for i, item in enumerate(items, 1))


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
