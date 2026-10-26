"""Тесты расчёта пиковых часов и прироста просмотров."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import MSK

from tiktok_manager.analytics import (
    DEFAULT,
    DEFAULT_PEAK_HOURS,
    MANUAL,
    STATS,
    Point,
    Profile,
    activity_profile,
    choose_peak_hours,
    daily_views,
    views_gained,
)

OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


def series(video_id: str, *pairs: tuple[datetime, int]) -> list[Point]:
    return [Point(video_id, moment, views) for moment, views in pairs]


def profile_of(points, created=None, tz=MSK) -> Profile:
    return activity_profile(points, created or {"v": OLD}, tz)


def test_gain_goes_to_local_hour():
    # 16:00–17:00 UTC — это 19:00–20:00 по Москве.
    profile = profile_of(series("v", (utc(20, 16), 100), (utc(20, 17), 200)))
    assert profile.views[19] == pytest.approx(100)
    assert profile.total == pytest.approx(100)


def test_gain_split_between_hours():
    profile = profile_of(series("v", (utc(20, 16, 30), 0), (utc(20, 17, 30), 60)))
    assert profile.views[19] == pytest.approx(30)
    assert profile.views[20] == pytest.approx(30)


def test_launch_hours_are_skipped():
    """Всплеск сразу после публикации говорит о времени публикации, а не об аудитории."""
    created = {"v": utc(20, 15)}
    points = series(
        "v",
        (utc(20, 15), 0),
        (utc(20, 16), 5000),  # первые часы — пропускаем
        (utc(20, 22), 6000),  # промежуток длиннее 3 часов — тоже
        (utc(20, 23), 6100),
    )
    profile = profile_of(points, created)
    assert profile.total == pytest.approx(100)
    assert profile.views[1] == pytest.approx(100)  # 22:00–23:00 UTC = 01:00–02:00 МСК


def test_launch_cut_inside_interval():
    """Промежуток пересекает конец «стартовых» часов — считаем только его хвост."""
    created = {"v": utc(20, 10)}  # стартовые часы до 16:00 UTC
    profile = profile_of(series("v", (utc(20, 15, 30), 0), (utc(20, 16, 30), 60)), created)
    assert profile.total == pytest.approx(30)
    assert profile.views[19] == pytest.approx(30)


def test_long_gaps_and_drops_are_ignored():
    points = series(
        "v",
        (utc(20, 0), 100),
        (utc(20, 5), 900),  # 5 часов без снимков
        (utc(20, 6), 850),  # просмотры «упали» (TikTok пересчитал)
        (utc(20, 7), 870),
    )
    profile = profile_of(points)
    assert profile.total == pytest.approx(20)


def test_fall_back_day_counts_repeated_hour():
    """В ночь перевода часов назад 02:00–03:00 бывает дважды."""
    berlin = ZoneInfo("Europe/Berlin")
    # 25.10.2026: 00:00–02:00 UTC — это 02:00 CEST и снова 02:00 CET.
    profile = profile_of(series("v", (utc(25, 0), 0), (utc(25, 2), 120)), tz=berlin)
    assert profile.views[2] == pytest.approx(120)
    assert profile.total == pytest.approx(120)


def test_half_hour_timezone():
    kolkata = ZoneInfo("Asia/Kolkata")  # UTC+5:30
    profile = profile_of(series("v", (utc(20, 0), 0), (utc(20, 1), 60)), tz=kolkata)
    assert profile.views[5] == pytest.approx(30)
    assert profile.views[6] == pytest.approx(30)


def test_profile_reliability_needs_views_and_time():
    hourly = [(utc(20, 0) + timedelta(hours=h), h * 10) for h in range(49)]
    assert profile_of(series("v", *hourly)).reliable
    short = [(utc(20, 0) + timedelta(hours=h), h * 100) for h in range(10)]
    assert not profile_of(series("v", *short)).reliable
    few_views = [(utc(20, 0) + timedelta(hours=h), h) for h in range(60)]
    assert not profile_of(series("v", *few_views)).reliable


def reliable_profile(views: dict[int, float]) -> Profile:
    hours = [0.0] * 24
    for hour, value in views.items():
        hours[hour] = value
    return Profile(views=tuple(hours), span=timedelta(days=7))


def test_manual_hours_win():
    peaks = choose_peak_hours(reliable_profile({20: 500}), (18, 12), count=3, min_gap_hours=3)
    assert (peaks.hours, peaks.source) == ((18, 12), MANUAL)


def test_default_hours_without_data():
    peaks = choose_peak_hours(Profile((0.0,) * 24, timedelta(0)), (), count=3, min_gap_hours=3)
    assert (peaks.hours, peaks.source) == (DEFAULT_PEAK_HOURS, DEFAULT)


def test_hot_hours_are_spaced_apart():
    views = {12: 300, 13: 320, 18: 380, 19: 400, 20: 390, 21: 350}
    peaks = choose_peak_hours(reliable_profile(views), (), count=3, min_gap_hours=3)
    assert peaks.source == STATS
    assert peaks.hours[0] in (19, 20)
    for first in peaks.hours:
        for second in peaks.hours:
            if first != second:
                assert min(abs(first - second), 24 - abs(first - second)) >= 3


def test_weak_hours_are_not_used():
    """Час слабее половины пика — «мёртвое» время, лучше перенести ролик."""
    peaks = choose_peak_hours(reliable_profile({19: 1000, 8: 100}), (), count=3, min_gap_hours=3)
    assert peaks.hours == (19,)


def test_single_spike_loses_to_broad_peak():
    peaks = choose_peak_hours(
        reliable_profile({3: 100, 19: 60, 20: 70, 21: 60}), (), count=1, min_gap_hours=3
    )
    assert peaks.hours == (20,)


def test_gap_wraps_around_midnight():
    """От 23:00 до 01:00 — два часа, а не двадцать два."""
    peaks = choose_peak_hours(reliable_profile({23: 1000, 1: 950}), (), count=3, min_gap_hours=3)
    assert peaks.hours == (23,)


def test_daily_views():
    created = {"old": OLD, "new": utc(21, 10)}
    points = [
        *series("old", (utc(20, 10), 1000), (utc(20, 20), 1100), (utc(21, 10), 1300)),
        *series("new", (utc(21, 11), 50), (utc(21, 12), 80)),
    ]
    gains = daily_views(points, created, MSK)
    # Просмотры «старого» ролика до первого снимка не считаем.
    assert gains[utc(20, 20).date()] == 100
    # Новый вышел при работающем сборе — его первые 50 просмотров свежие.
    assert gains[utc(21, 10).date()] == 200 + 50 + 30


def test_views_gained():
    since = utc(21, 0)
    created = {"old": OLD, "new": utc(21, 6)}
    points = [
        *series("old", (utc(21, 1), 500), (utc(21, 9), 650)),
        *series("new", (utc(21, 7), 40), (utc(21, 9), 90)),
    ]
    assert views_gained(points, created, since) == {"old": 150, "new": 90}
