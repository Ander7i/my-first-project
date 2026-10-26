"""Тесты форматирования отчётов."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import MSK, NOW

from tiktok_manager.analytics import DEFAULT_PEAK_HOURS, MANUAL, STATS, PeakHours, Profile
from tiktok_manager.report import bar, format_peak, num, signed, table, when
from tiktok_manager.slots import SlotRules

RULES = SlotRules(lead_minutes=60, max_per_day=2, min_gap_hours=3)


def test_numbers():
    assert num(1234567) == "1 234 567"
    assert num(12.6) == "13"
    assert signed(1500) == "+1 500"
    assert signed(-3) == "−3"
    assert signed(0) == "0"


def test_bar_has_half_steps():
    assert bar(10, 10, width=4) == "████"
    assert bar(5, 10, width=4) == "██"
    assert bar(6.25, 10, width=4) == "██▌"
    assert bar(0, 10) == ""
    assert bar(5, 0) == ""


def test_when_shows_year_only_if_needed():
    assert when(NOW, MSK, now=NOW) == "пн 26.10 12:00"
    next_year = datetime(2027, 1, 4, 9, tzinfo=timezone.utc)
    assert when(next_year, MSK, now=NOW) == "пн 04.01.2027 12:00"


def test_table_alignment():
    text = table(("Имя", "Число"), [("а", "1"), ("ббб", "100")], right=(1,))
    assert text.splitlines() == ["Имя  Число", "а        1", "ббб    100"]


def test_peak_report_marks_peaks():
    views = [0.0] * 24
    views[19], views[13] = 600, 400
    profile = Profile(tuple(views), timedelta(days=5))
    text = format_peak(
        profile,
        PeakHours((19, 13), STATS),
        [NOW + timedelta(hours=6)],
        rules=RULES,
        days=28,
        tz=MSK,
        now=NOW,
    )
    assert "19:00  ████████████████████████  60.0%  ◀ пик" in text
    assert "Пиковые часы по вашей статистике: 19:00, 13:00." in text
    assert "Публикую до 2 роликов в день: в 12:00 и 18:00 — за 60 мин до пика" in text
    assert "Ближайшие свободные слоты: пн 26.10 18:00" in text


def test_peak_report_for_manual_hours():
    text = format_peak(
        Profile((0.0,) * 24, timedelta(0)),
        PeakHours((20,), MANUAL),
        [],
        rules=SlotRules(lead_minutes=0, max_per_day=1, min_gap_hours=3),
        days=28,
        tz=MSK,
        now=NOW,
    )
    assert "заданы вручную в PEAK_HOURS: 20:00" in text
    assert "Публикую до 1 ролика в день: в 20:00." in text


def test_peak_report_explains_missing_data():
    text = format_peak(
        Profile((0.0,) * 24, timedelta(hours=3)),
        PeakHours(DEFAULT_PEAK_HOURS, "default"),
        [],
        rules=RULES,
        days=28,
        tz=MSK,
        now=NOW,
    )
    assert "Данных пока мало" in text
    assert "TikTok Studio" in text
