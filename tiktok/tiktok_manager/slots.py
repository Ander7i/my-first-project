"""Подбор времени публикации: ближайший свободный пиковый час."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo

from .analytics import hour_distance

# Ближе этого слот не назначаем: нужно время, чтобы загрузить ролик.
MIN_LEAD_TIME = timedelta(minutes=5)
# Дальше этого свободное время не ищем.
HORIZON_DAYS = 90
# Даже без интервала два ролика в одну минуту не ставим.
MIN_SPACING = timedelta(minutes=1)


class NoFreeSlot(RuntimeError):
    """В очереди не осталось места."""


@dataclass(frozen=True)
class SlotRules:
    lead_minutes: int  # за сколько минут до пика публиковать
    max_per_day: int
    min_gap_hours: int


def daily_hours(hours: Sequence[int], rules: SlotRules) -> list[int]:
    """Часы публикаций на каждый день, по порядку на часах.

    Из пиков, идущих от лучшего к худшему, берём лучшие — не больше
    max_per_day и не ближе min_gap_hours друг к другу.
    """
    chosen: list[int] = []
    for hour in hours:
        if all(hour_distance(hour, other) >= rules.min_gap_hours for other in chosen):
            chosen.append(hour)
            if len(chosen) == rules.max_per_day:
                break
    return sorted(chosen)


def next_slot(
    now: datetime,
    hours: Sequence[int],
    taken: Sequence[datetime],
    tz: tzinfo,
    rules: SlotRules,
) -> datetime:
    """Ближайшее свободное время публикации, в UTC.

    Ролик занимает самый ранний свободный из ежедневных пиковых часов, поэтому
    ролики выходят в том порядке, в каком их добавили. Слот должен быть ещё
    впереди, не ближе min_gap_hours к другим публикациям и не выходить за лимит
    роликов в день. Публикуем за lead_minutes до пика: ролику нужно время на
    обработку и первые показы.
    """
    if not hours:
        raise NoFreeSlot("Не заданы пиковые часы.")
    earliest = now + MIN_LEAD_TIME
    lead = timedelta(minutes=rules.lead_minutes)
    gap = max(timedelta(hours=rules.min_gap_hours), MIN_SPACING)
    per_day = Counter(moment.astimezone(tz).date() for moment in taken)
    today = now.astimezone(tz).date()
    for offset in range(HORIZON_DAYS + 1):
        day = today + timedelta(days=offset)
        for hour in daily_hours(hours, rules):
            # Пик переводим в UTC до вычитания: арифметика в местном времени
            # сбивается на переходе на летнее время.
            slot = datetime.combine(day, time(hour), tzinfo=tz).astimezone(timezone.utc) - lead
            if slot < earliest:
                continue
            if per_day[slot.astimezone(tz).date()] >= rules.max_per_day:
                continue
            if any(abs(slot - moment) < gap for moment in taken):
                continue
            return slot
    raise NoFreeSlot(
        f"На ближайшие {HORIZON_DAYS} дней свободного времени нет. Уберите часть роликов "
        "из очереди или увеличьте MAX_POSTS_PER_DAY."
    )


def upcoming_slots(
    now: datetime,
    hours: Sequence[int],
    taken: Sequence[datetime],
    tz: tzinfo,
    rules: SlotRules,
    *,
    count: int,
) -> list[datetime]:
    """Несколько ближайших свободных слотов подряд — как если бы ролики добавляли по одному."""
    busy = list(taken)
    slots = []
    for _ in range(count):
        slot = next_slot(now, hours, busy, tz, rules)
        slots.append(slot)
        busy.append(slot)
    return slots
