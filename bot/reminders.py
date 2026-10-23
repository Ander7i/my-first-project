"""Напоминания по срокам.

Зачем: человек берёт задание и забывает. Для займа это стоит ему денег в
день просрочки, для ИП — остаётся незакрытый статус. Одного сообщения при
взятии задания недостаточно.

Цикл просыпается раз в час, смотрит на активные задания и отправляет
каждое напоминание ровно один раз. Отметка в базе ставится ДО отправки,
поэтому даже два одновременно запущенных бота не пришлют дубль.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import Bot

from . import texts
from .catalog import check_by_code
from .config import Config
from .dates import days_until, from_iso
from .db import Database
from .utils import send_safe

log = logging.getLogger(__name__)

# Как часто просыпаться. Срок меряется днями, поэтому час — с запасом.
CHECK_INTERVAL = 3600

# Аудитория русскоязычная, поэтому тихие часы считаем по Москве (UTC+3).
# Напоминание в три ночи раздражает сильнее, чем помогает.
LOCAL_OFFSET = timedelta(hours=3)
QUIET_FROM = 22
QUIET_UNTIL = 9

# За сколько дней до срока напоминать. Ключ — пометка в базе.
DEADLINE_STEPS: tuple[tuple[str, int], ...] = (
    ("d3", 3),
    ("d1", 1),
    ("d0", 0),
)
OVERDUE_KIND = "overdue"
FOLLOWUP_KIND = "followup"


def is_quiet_hour(now: datetime | None = None) -> bool:
    """Ночь по местному времени — напоминания ждут утра."""
    moment = (now or datetime.now(UTC)) + LOCAL_OFFSET
    return moment.hour >= QUIET_FROM or moment.hour < QUIET_UNTIL


async def run_reminders(bot: Bot, db: Database, config: Config) -> None:
    """Бесконечный цикл напоминаний. Запускается как фоновая задача."""
    log.info("Напоминания запущены, проверка раз в %s с", CHECK_INTERVAL)
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            if is_quiet_hour():
                continue
            sent = await run_once(bot, db, config)
            if sent:
                log.info("Отправлено напоминаний: %s", sent)
        except asyncio.CancelledError:
            log.info("Напоминания остановлены")
            raise
        except Exception as exc:
            # Цикл не должен умирать из-за одной ошибки: иначе напоминания
            # тихо перестанут работать, и никто этого не заметит.
            log.exception("Ошибка в цикле напоминаний: %s", exc)


async def run_once(bot: Bot, db: Database, config: Config) -> int:
    """Один проход. Возвращает число отправленных напоминаний."""
    return await _remind_deadlines(bot, db) + await _remind_followups(bot, db, config)


async def _remind_deadlines(bot: Bot, db: Database) -> int:
    sent = 0
    for row in await db.assignments_in_work():
        due = from_iso(row["due_date"])
        if due is None:
            continue

        left = days_until(due)
        kind = _deadline_kind(left)
        if kind is None:
            continue

        # Отметка до отправки: лучше пропустить напоминание при сбое, чем
        # прислать его человеку дважды.
        if not await db.mark_reminder(row["id"], kind):
            continue

        if kind == OVERDUE_KIND:
            text = texts.REMINDER_OVERDUE.format(title=escape(row["title"]))
        else:
            text = texts.REMINDER_DEADLINE.format(
                title=escape(row["title"]),
                bank=escape(row["bank"] or "—"),
                payment=row["payment"],
                when=_when_phrase(left),
            )

        if await send_safe(bot, row["user_id"], text, db=db):
            sent += 1
    return sent


def _deadline_kind(days_left: int) -> str | None:
    """Какое напоминание пора отправить при таком остатке дней."""
    if days_left < 0:
        # Напоминаем о просрочке один раз, на следующий день после срока.
        return OVERDUE_KIND if days_left == -1 else None
    for kind, threshold in DEADLINE_STEPS:
        if days_left == threshold:
            return kind
    return None


def _when_phrase(days_left: int) -> str:
    if days_left == 0:
        return "сегодня"
    if days_left == 1:
        return "завтра"
    return f"через {days_left} дн."


async def _remind_followups(bot: Bot, db: Database, config: Config) -> int:
    """Напоминания о деле, которое осталось после сдачи отчёта."""
    sent = 0
    for row in await db.assignments_for_followup():
        check = check_by_code(row["check_code"])
        if check is None or not check.followup_days or not check.followup_text:
            continue

        decided = _parse_stamp(row["decided_at"])
        if decided is None:
            continue
        if (datetime.now(UTC) - decided).days < check.followup_days:
            continue

        if not await db.mark_reminder(row["id"], FOLLOWUP_KIND):
            continue

        text = check.followup_text.format(title=escape(row["title"]))
        if config.support_contact:
            text += f"\n\nСвязь: {escape(config.support_contact)}"

        if await send_safe(bot, row["user_id"], text, db=db):
            sent += 1
    return sent


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
