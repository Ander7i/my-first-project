"""Тесты подбора времени публикации."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import MSK

from tiktok_manager.slots import (
    NoFreeSlot,
    SlotRules,
    daily_hours,
    next_slot,
    upcoming_slots,
)

RULES = SlotRules(lead_minutes=60, max_per_day=2, min_gap_hours=3)


def msk(day: int, hour: int, minute: int = 0) -> datetime:
    """Время по Москве (UTC+3) в UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=MSK).astimezone(timezone.utc)


def slot(now, hours, taken=(), rules=RULES, tz=MSK):
    return next_slot(now, hours, list(taken), tz, rules)


def test_publishes_lead_minutes_before_peak():
    assert slot(msk(26, 12), (19,)) == msk(26, 18)


def test_passed_peak_moves_to_tomorrow():
    assert slot(msk(26, 20), (19,)) == msk(27, 18)


def test_needs_time_to_upload():
    """Слот через 3 минуты — уже поздно, нужно время на загрузку."""
    assert slot(msk(26, 17, 57), (19,)) == msk(27, 18)


def test_one_post_a_day_takes_the_best_hour():
    """Пик в 19:00 сильнее, чем в 13:00, — при одном ролике в день берём его."""
    rules = SlotRules(lead_minutes=60, max_per_day=1, min_gap_hours=3)
    assert slot(msk(26, 9), (19, 13), rules=rules) == msk(26, 18)


def test_videos_go_out_in_order_added():
    """Два ролика в день — в два лучших часа, и первый добавленный выходит первым."""
    first = slot(msk(26, 9), (19, 13))
    second = slot(msk(26, 9), (19, 13), taken=[first])
    assert (first, second) == (msk(26, 12), msk(26, 18))


def test_daily_hours():
    rules = SlotRules(lead_minutes=0, max_per_day=2, min_gap_hours=3)
    assert daily_hours((19, 20, 13, 16), rules) == [13, 19]
    # 01:00 всего в двух часах от 23:00 через полночь.
    assert daily_hours((23, 1, 18), rules) == [18, 23]
    assert daily_hours((19, 13), SlotRules(0, 5, 0)) == [13, 19]


def test_daily_limit():
    rules = SlotRules(lead_minutes=60, max_per_day=1, min_gap_hours=3)
    assert slot(msk(26, 9), (19, 13), taken=[msk(26, 18)], rules=rules) == msk(27, 18)


def test_gap_between_posts():
    # 20:00 − 60 мин = 19:00, всего через час после ролика в 18:00.
    assert slot(msk(26, 9), (19, 20), taken=[msk(26, 18)]) == msk(27, 18)


def test_free_earlier_slot_is_filled():
    """Ролик из 12:00 удалили — новый займёт освободившееся время."""
    assert slot(msk(26, 9), (19, 13), taken=[msk(26, 18)]) == msk(26, 12)


def test_no_two_posts_at_same_minute_even_without_gap():
    rules = SlotRules(lead_minutes=60, max_per_day=5, min_gap_hours=0)
    assert slot(msk(26, 9), (19,), taken=[msk(26, 18)], rules=rules) == msk(27, 18)


def test_lead_can_cross_midnight():
    """Пик в 00:00 — публикация в 23:00 предыдущего дня, и считается она за тот день."""
    rules = SlotRules(lead_minutes=60, max_per_day=1, min_gap_hours=3)
    assert slot(msk(26, 9), (0,), rules=rules) == msk(26, 23)
    assert slot(msk(26, 9), (0,), taken=[msk(26, 23)], rules=rules) == msk(27, 23)


def test_spring_forward_day():
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 3, 29, 6, tzinfo=timezone.utc)
    # 29.03.2026 Германия уже на летнем времени (UTC+2): пик 19:00 — это 17:00 UTC.
    result = slot(now, (19,), tz=berlin)
    assert result == datetime(2026, 3, 29, 16, tzinfo=timezone.utc)


def test_full_queue():
    rules = SlotRules(lead_minutes=0, max_per_day=1, min_gap_hours=3)
    start = msk(26, 9)
    taken = [msk(26, 19) + timedelta(days=day) for day in range(120)]
    with pytest.raises(NoFreeSlot, match="MAX_POSTS_PER_DAY"):
        slot(start, (19,), taken=taken, rules=rules)


def test_no_hours():
    with pytest.raises(NoFreeSlot):
        slot(msk(26, 9), ())


def test_upcoming_slots_fill_in_order():
    slots = upcoming_slots(msk(26, 9), (19, 13), [], MSK, RULES, count=4)
    assert slots == [msk(26, 12), msk(26, 18), msk(27, 12), msk(27, 18)]
