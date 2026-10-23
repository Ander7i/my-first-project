"""Разбор и показ сроков заданий.

Срок нужен машинно-читаемым, иначе по нему нельзя напоминать. Админ
вводит дату как удобно, здесь она превращается в ISO-дату для базы и в
человеческую строку для показа.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

# «30.10.2026», «30.10.26», «30.10», «30/10»
_NUMERIC = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2}|\d{4}))?$")
# «через 7 дней», «7 дней», «7»
_IN_DAYS = re.compile(r"^(?:через\s+)?(\d{1,3})(?:\s*(?:дн|дней|день|дня))?$")

# Сколько дней вперёд считаем осмысленным сроком задания.
MAX_HORIZON_DAYS = 365


def today() -> date:
    return datetime.now(UTC).date()


def parse_due_date(raw: str, *, base: date | None = None) -> date | None:
    """Превращает ввод админа в дату. None — разобрать не удалось.

    Понимает «30.10.2026», «30.10», «через 7 дней», «7», «завтра».
    Дата без года относится к ближайшему будущему: если в этом году она
    уже прошла, берётся следующий год.
    """
    value = " ".join((raw or "").strip().lower().replace("ё", "е").split())
    if not value:
        return None

    base = base or today()

    if value in ("сегодня", "today"):
        return base
    if value in ("завтра", "tomorrow"):
        return base + timedelta(days=1)
    if value == "послезавтра":
        return base + timedelta(days=2)

    # «до 30 октября» — предлог игнорируем.
    value = value.removeprefix("до ").strip()

    match = _NUMERIC.match(value)
    if match:
        day, month, year_part = match.groups()
        day, month = int(day), int(month)
        if year_part is None:
            candidate = _safe_date(base.year, month, day)
            if candidate is None:
                return None
            # Дата без года не должна оказаться в прошлом.
            if candidate < base:
                candidate = _safe_date(base.year + 1, month, day)
            return candidate
        year = int(year_part)
        if year < 100:
            year += 2000
        return _safe_date(year, month, day)

    match = _IN_DAYS.match(value)
    if match:
        days = int(match.group(1))
        if days == 0 or days > MAX_HORIZON_DAYS:
            return None
        return base + timedelta(days=days)

    # «30 октября» и «30 октября 2026»
    parts = value.split()
    if len(parts) in (2, 3) and parts[0].isdigit():
        for index, name in enumerate(MONTHS_GENITIVE, 1):
            if parts[1].startswith(name[:4]):
                day = int(parts[0])
                if len(parts) == 3 and parts[2].isdigit():
                    return _safe_date(int(parts[2]), index, day)
                candidate = _safe_date(base.year, index, day)
                if candidate is None:
                    return None
                if candidate < base:
                    candidate = _safe_date(base.year + 1, index, day)
                return candidate

    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    """Собирает дату, не падая на 31 февраля и прочей ерунде."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def is_sane_due_date(value: date, *, base: date | None = None) -> bool:
    """Отсеивает прошлое и слишком далёкое будущее."""
    base = base or today()
    return base <= value <= base + timedelta(days=MAX_HORIZON_DAYS)


def format_due_date(value: date) -> str:
    """«до 30 октября» или «до 30 октября 2027», если год не текущий."""
    text = f"до {value.day} {MONTHS_GENITIVE[value.month - 1]}"
    if value.year != today().year:
        text += f" {value.year}"
    return text


def to_iso(value: date) -> str:
    return value.isoformat()


def from_iso(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def days_until(value: date, *, base: date | None = None) -> int:
    """Сколько дней осталось. Отрицательное — срок прошёл."""
    return (value - (base or today())).days
