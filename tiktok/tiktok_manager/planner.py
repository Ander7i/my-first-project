"""Связка базы и расчётов: пиковые часы аккаунта и время следующей публикации."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .analytics import (
    MAX_INTERVAL,
    PeakHours,
    Point,
    Profile,
    activity_profile,
    choose_peak_hours,
    daily_views,
    views_gained,
)
from .config import Config
from .db import Database, from_iso
from .slots import SlotRules, next_slot, upcoming_slots

# За сколько дней берём данные для пиковых часов: привычки аудитории меняются.
PROFILE_DAYS = 28


def slot_rules(config: Config) -> SlotRules:
    return SlotRules(
        lead_minutes=config.lead_minutes,
        max_per_day=config.max_posts_per_day,
        min_gap_hours=config.min_gap_hours,
    )


def peak_hours(
    config: Config, db: Database, open_id: str, now: datetime
) -> tuple[PeakHours, Profile]:
    points, created = _points(db, open_id, now - timedelta(days=PROFILE_DAYS))
    profile = activity_profile(points, created, config.timezone)
    peaks = choose_peak_hours(
        profile,
        config.peak_hours,
        # Часов берём с запасом: какие-то окажутся заняты или уже в прошлом.
        count=max(3, config.max_posts_per_day),
        min_gap_hours=config.min_gap_hours,
    )
    return peaks, profile


def find_slot(
    config: Config, db: Database, open_id: str, now: datetime, *, exclude_id: int | None = None
) -> tuple[datetime, PeakHours]:
    """Ближайшее свободное пиковое время для нового ролика."""
    peaks, _ = peak_hours(config, db, open_id, now)
    taken = db.taken_slots(open_id, now - timedelta(days=1), exclude_id=exclude_id)
    return next_slot(now, peaks.hours, taken, config.timezone, slot_rules(config)), peaks


def free_slots(
    config: Config, db: Database, open_id: str, now: datetime, peaks: PeakHours, *, count: int
) -> list[datetime]:
    taken = db.taken_slots(open_id, now - timedelta(days=1))
    return upcoming_slots(now, peaks.hours, taken, config.timezone, slot_rules(config), count=count)


def views_by_day(
    config: Config, db: Database, open_id: str, now: datetime, *, days: int
) -> list[tuple[date, int | None]]:
    """Прирост просмотров за каждый из последних days дней. None — снимков в тот день не было."""
    tz = config.timezone
    first_day = now.astimezone(tz).date() - timedelta(days=days - 1)
    start = datetime.combine(first_day, time(0), tzinfo=tz).astimezone(timezone.utc)
    # Берём и снимок до полуночи — от него считается прирост первого дня.
    points, created = _points(db, open_id, start - MAX_INTERVAL)
    gains = daily_views(points, created, tz)
    seen = {point.taken_at.astimezone(tz).date() for point in points}
    result = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        result.append((day, gains.get(day, 0) if day in seen else None))
    return result


def recent_gains(db: Database, open_id: str, since: datetime) -> dict[str, int]:
    points, created = _points(db, open_id, since)
    return views_gained(points, created, since)


def _points(db: Database, open_id: str, since: datetime) -> tuple[list[Point], dict[str, datetime]]:
    rows = db.video_points(open_id, since)
    points = [Point(row["video_id"], from_iso(row["taken_at"]), row["view_count"]) for row in rows]
    created = {row["video_id"]: from_iso(row["created_at"]) for row in rows}
    return points, created
