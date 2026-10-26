"""Аналитика по снимкам статистики: в какие часы смотрят ролики и сколько за день.

Планировщик раз в час сохраняет счётчики просмотров всех роликов. Прирост
между двумя снимками раскладывается по часам суток — так видно, когда
аудитория действительно смотрит ролики именно этого аккаунта.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from itertools import pairwise

# Первые часы ролик набирает просмотры из-за самой публикации, а не потому,
# что аудитория в эти часы онлайн. Если их учитывать, «пиком» окажется время,
# в которое вы обычно и публикуете.
LAUNCH_HOURS = 6
# Прирост за длинный промежуток (сбор не работал) честно по часам не разложить.
MAX_INTERVAL = timedelta(hours=3)
# С меньшим объёмом данных профиль — ещё шум.
MIN_PROFILE_VIEWS = 200
MIN_PROFILE_SPAN = timedelta(hours=48)
# Часы слабее этой доли от пика в расписание не берём: лучше перенести ролик
# на завтра, чем выложить его в «мёртвое» время.
HOT_SHARE = 0.5
# Пока своих данных нет: вечер, обед и после учёбы. Разнесены на 3 часа,
# чтобы хватало на две публикации в день.
DEFAULT_PEAK_HOURS = (19, 13, 16)

# Откуда взяты пиковые часы.
MANUAL = "manual"
STATS = "stats"
DEFAULT = "default"


@dataclass(frozen=True)
class Point:
    """Счётчик просмотров ролика в момент снимка."""

    video_id: str
    taken_at: datetime
    views: int


@dataclass(frozen=True)
class Profile:
    """Сколько просмотров пришлось на каждый час суток (в поясе TIMEZONE)."""

    views: tuple[float, ...]
    span: timedelta  # сколько времени покрывают снимки

    @property
    def total(self) -> float:
        return sum(self.views)

    @property
    def shares(self) -> tuple[float, ...]:
        total = self.total
        if not total:
            return (0.0,) * 24
        return tuple(value / total for value in self.views)

    @property
    def reliable(self) -> bool:
        return self.total >= MIN_PROFILE_VIEWS and self.span >= MIN_PROFILE_SPAN


@dataclass(frozen=True)
class PeakHours:
    hours: tuple[int, ...]  # от лучшего к худшему
    source: str  # MANUAL, STATS или DEFAULT


def activity_profile(
    points: Iterable[Point], created: Mapping[str, datetime], tz: tzinfo
) -> Profile:
    series = _by_video(points)
    views = [0.0] * 24
    for video_id, video_points in series.items():
        published = created.get(video_id)
        launch_end = None if published is None else published + timedelta(hours=LAUNCH_HOURS)
        for before, after in pairwise(video_points):
            gained = after.views - before.views
            interval = after.taken_at - before.taken_at
            if gained <= 0 or not timedelta(0) < interval <= MAX_INTERVAL:
                continue
            start = before.taken_at
            if launch_end is not None and launch_end > start:
                start = launch_end
            if start >= after.taken_at:
                continue
            # Внутри промежутка считаем просмотры равномерными.
            _spread(views, start, after.taken_at, gained / interval.total_seconds(), tz)

    stamps = [point.taken_at for video_points in series.values() for point in video_points]
    span = max(stamps) - min(stamps) if stamps else timedelta(0)
    return Profile(views=tuple(views), span=span)


def daily_views(
    points: Iterable[Point], created: Mapping[str, datetime], tz: tzinfo
) -> dict[date, int]:
    """Прирост просмотров по дням (в поясе tz).

    Просмотры, набранные до первого снимка, неизвестно когда пришли, и их не
    считаем. Кроме роликов, вышедших уже при работающем сборе: у них все
    просмотры свежие.
    """
    series = _by_video(points)
    if not series:
        return {}
    start = min(video_points[0].taken_at for video_points in series.values())
    gains: dict[date, int] = defaultdict(int)
    for video_id, video_points in series.items():
        first = video_points[0]
        published = created.get(video_id)
        if published is not None and published >= start:
            gains[first.taken_at.astimezone(tz).date()] += first.views
        for before, after in pairwise(video_points):
            if after.views > before.views:
                gains[after.taken_at.astimezone(tz).date()] += after.views - before.views
    return dict(gains)


def views_gained(
    points: Iterable[Point], created: Mapping[str, datetime], since: datetime
) -> dict[str, int]:
    """Сколько просмотров каждый ролик набрал с момента since."""
    gained = {}
    for video_id, video_points in _by_video(points).items():
        published = created.get(video_id)
        first, last = video_points[0], video_points[-1]
        if published is not None and published >= since:
            gained[video_id] = last.views
        else:
            gained[video_id] = max(last.views - first.views, 0)
    return gained


def choose_peak_hours(
    profile: Profile, manual: Sequence[int], *, count: int, min_gap_hours: int
) -> PeakHours:
    """Часы для публикаций: заданные вручную, иначе по статистике, иначе по умолчанию."""
    if manual:
        return PeakHours(tuple(manual), MANUAL)
    if profile.reliable:
        hours = _hot_hours(profile.views, count, min_gap_hours)
        if hours:
            return PeakHours(hours, STATS)
    return PeakHours(DEFAULT_PEAK_HOURS, DEFAULT)


def _hot_hours(views: Sequence[float], count: int, min_gap_hours: int) -> tuple[int, ...]:
    """Самые «горячие» часы, разнесённые хотя бы на min_gap_hours друг от друга."""
    # Сглаживаем соседями: случайный всплеск в одном часе не должен стать пиком.
    smoothed = [
        0.25 * views[hour - 1] + 0.5 * views[hour] + 0.25 * views[(hour + 1) % 24]
        for hour in range(24)
    ]
    top = max(smoothed)
    if top <= 0:
        return ()
    ranked = sorted(
        (hour for hour in range(24) if smoothed[hour] >= top * HOT_SHARE),
        key=lambda hour: (-smoothed[hour], hour),
    )
    chosen: list[int] = []
    for hour in ranked:
        if all(hour_distance(hour, other) >= min_gap_hours for other in chosen):
            chosen.append(hour)
            if len(chosen) == count:
                break
    return tuple(chosen)


def hour_distance(first: int, second: int) -> int:
    """Расстояние между часами по кругу: от 23:00 до 01:00 — два часа, а не двадцать два."""
    diff = abs(first - second) % 24
    return min(diff, 24 - diff)


def _spread(views: list[float], start: datetime, end: datetime, rate: float, tz: tzinfo) -> None:
    """Раскладывает просмотры промежутка [start, end) по часам суток."""
    cursor = start
    while cursor < end:
        local = cursor.astimezone(tz)
        hour_start = local.replace(minute=0, second=0, microsecond=0)
        # Границу часа считаем в UTC: тогда переход на летнее время и пояса
        # со сдвигом в полчаса не ломают арифметику.
        boundary = min(hour_start.astimezone(timezone.utc) + timedelta(hours=1), end)
        views[local.hour] += rate * (boundary - cursor).total_seconds()
        cursor = boundary


def _by_video(points: Iterable[Point]) -> dict[str, list[Point]]:
    series: dict[str, list[Point]] = defaultdict(list)
    for point in points:
        series[point.video_id].append(point)
    for video_points in series.values():
        video_points.sort(key=lambda point: point.taken_at)
    return dict(series)
