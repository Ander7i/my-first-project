"""Текстовые отчёты для консоли: статистика, пиковые часы, очередь."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, tzinfo
from pathlib import Path
from sqlite3 import Row

from . import db as store
from .analytics import (
    DEFAULT,
    MANUAL,
    MIN_PROFILE_SPAN,
    MIN_PROFILE_VIEWS,
    STATS,
    PeakHours,
    Profile,
)
from .db import from_iso
from .slots import SlotRules, daily_hours

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
BAR_WIDTH = 24

STATUS_LABELS = {
    store.SCHEDULED: "ждёт",
    store.UPLOADING: "загружается",
    store.PROCESSING: "обрабатывается",
    store.PUBLISHED: "опубликован",
    store.INBOX_SENT: "во «Входящих»",
    store.FAILED: "ошибка",
}

PEAK_SOURCES = {
    STATS: "пик по вашей статистике",
    MANUAL: "пик из PEAK_HOURS",
    DEFAULT: "пик по умолчанию — своих данных пока мало",
}


def num(value: float) -> str:
    """12345 → «12 345»."""
    return f"{round(value):,}".replace(",", " ")


def signed(value: float) -> str:
    rounded = round(value)
    if rounded > 0:
        return f"+{num(rounded)}"
    if rounded < 0:
        return f"−{num(-rounded)}"
    return "0"


def when(moment: datetime, tz: tzinfo, *, now: datetime) -> str:
    """«пн 26.10 18:00»; год — только если он не текущий."""
    local = moment.astimezone(tz)
    day = f"{local:%d.%m}" if local.year == now.astimezone(tz).year else f"{local:%d.%m.%Y}"
    return f"{WEEKDAYS[local.weekday()]} {day} {local:%H:%M}"


def day_label(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"


def zone_name(tz: tzinfo) -> str:
    return str(getattr(tz, "key", None) or tz)


def bar(value: float, maximum: float, width: int = BAR_WIDTH) -> str:
    """Полоска для гистограммы с точностью до полсимвола."""
    if maximum <= 0 or value <= 0:
        return ""
    halves = round(value / maximum * width * 2)
    return "█" * (halves // 2) + ("▌" if halves % 2 else "")


def _join(items: Sequence[str]) -> str:
    """«a», «a и b», «a, b и c»."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " и " + items[-1]


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], *, right: Sequence[int] = ()
) -> str:
    """Таблица с выровненными колонками. right — номера колонок, прижатых вправо."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def line(cells: Sequence[str]) -> str:
        parts = [
            cell.rjust(widths[index]) if index in right else cell.ljust(widths[index])
            for index, cell in enumerate(cells)
        ]
        return "  ".join(parts).rstrip()

    return "\n".join([line(headers), *(line(row) for row in rows)])


def format_stats(
    *,
    name: str,
    snapshot_at: datetime,
    latest: Row | None,
    baseline: Row | None,
    videos: Sequence[Row],
    gains: Mapping[str, int],
    daily: Sequence[tuple[date, int | None]],
    top: int,
    tz: tzinfo,
    now: datetime,
) -> str:
    lines = [f"Аккаунт «{name}» · данные на {when(snapshot_at, tz, now=now)}", ""]

    known_days = [views for _, views in daily if views is not None]
    period = f"за {len(daily)} дн."
    rows = []
    if latest is not None:
        # Сравнивать есть с чем, только если старый снимок — не тот же самый.
        before = baseline
        if before is not None and before["taken_at"] == latest["taken_at"]:
            before = None
        if before is not None:
            period = f"с {when(from_iso(before['taken_at']), tz, now=now)}"
        for title, key in (
            ("Подписчики", "follower_count"),
            ("Лайки", "likes_count"),
            ("Ролики", "video_count"),
        ):
            change = signed(latest[key] - before[key]) if before is not None else ""
            rows.append((title, num(latest[key]), change))
    total_views = sum(video["view_count"] for video in videos)
    rows.append(("Просмотры", num(total_views), signed(sum(known_days)) if known_days else ""))
    lines.append(table(("", "сейчас", period), rows, right=(1, 2)))
    if latest is None:
        lines.append(
            "Подписчиков и лайков нет: у подключения нет права user.info.stats. "
            "Добавьте его в Scopes приложения и войдите заново."
        )

    if known_days:
        lines += ["", "Просмотры по дням:"]
        peak = max(known_days)
        # Дни до начала сбора статистики не показываем — там всегда пусто.
        first_known = next(index for index, (_, views) in enumerate(daily) if views is not None)
        for day, views in daily[first_known:]:
            if views is None:
                lines.append(f"{day_label(day)}  нет данных")
            else:
                lines.append(f"{day_label(day)}  {bar(views, peak):<{BAR_WIDTH}}  {num(views):>8}")

    shown = list(videos[:top])
    if shown:
        lines += ["", f"Последние ролики ({len(shown)} из {len(videos)}):"]
        video_rows = []
        for video in shown:
            views = video["view_count"]
            reactions = video["like_count"] + video["comment_count"] + video["share_count"]
            engagement = f"{reactions / views * 100:.1f}%" if views else "—"
            video_rows.append(
                (
                    f"{from_iso(video['created_at']).astimezone(tz):%d.%m %H:%M}",
                    num(views),
                    signed(gains.get(video["id"], 0)) if video["id"] in gains else "",
                    num(video["like_count"]),
                    num(video["comment_count"]),
                    num(video["share_count"]),
                    engagement,
                    shorten(video["title"] or video["share_url"], 40),
                )
            )
        lines.append(
            table(
                ("Вышел", "Просмотры", "За 24 ч", "Лайки", "Комм.", "Репосты", "ER", "Ролик"),
                video_rows,
                right=(1, 2, 3, 4, 5, 6),
            )
        )
        lines.append("ER — лайки, комментарии и репосты в процентах от просмотров.")
    else:
        lines += ["", "Публичных роликов пока нет."]
    return "\n".join(lines)


def format_peak(
    profile: Profile,
    peaks: PeakHours,
    slots: Sequence[datetime],
    *,
    rules: SlotRules,
    days: int,
    tz: tzinfo,
    now: datetime,
) -> str:
    lines = []
    hours_text = ", ".join(f"{hour}:00" for hour in peaks.hours)
    observed = round(profile.span.total_seconds() / 3600)

    if profile.total > 0:
        lines.append(
            f"Когда смотрят ваши ролики — по часам ({zone_name(tz)}), данные за {days} дн.:"
        )
        shares = profile.shares
        top = max(shares)
        for hour in range(24):
            mark = "  ◀ пик" if hour in peaks.hours and peaks.source != DEFAULT else ""
            lines.append(
                f"{hour:02d}:00  {bar(shares[hour], top):<{BAR_WIDTH}}  "
                f"{shares[hour] * 100:4.1f}%{mark}"
            )
        lines += [f"Учтено просмотров: {num(profile.total)} за {observed} ч наблюдений.", ""]

    if peaks.source == MANUAL:
        lines.append(f"Пиковые часы заданы вручную в PEAK_HOURS: {hours_text}.")
    elif peaks.source == DEFAULT:
        need_hours = round(MIN_PROFILE_SPAN.total_seconds() / 3600)
        lines += [
            (
                f"Данных пока мало: учтено {num(profile.total)} просмотров за {observed} ч, "
                f"а нужно от {MIN_PROFILE_VIEWS} просмотров и {need_hours} ч сбора. Держите "
                "запущенным планировщик (run) — он снимает статистику раз в час."
            ),
            f"Пока беру часы по умолчанию: {hours_text}.",
            (
                "Точные часы есть в TikTok Studio → Аналитика → Подписчики → «Активность "
                "подписчиков» — их можно вписать в PEAK_HOURS."
            ),
        ]
    else:
        lines.append(f"Пиковые часы по вашей статистике: {hours_text}.")

    # Какие из пиков реально идут в расписание и во сколько.
    minutes = sorted(
        (hour * 60 - rules.lead_minutes) % 1440 for hour in daily_hours(peaks.hours, rules)
    )
    times = [f"{value // 60}:{value % 60:02d}" for value in minutes]
    videos = "ролика" if rules.max_per_day == 1 else "роликов"
    plan = f"Публикую до {rules.max_per_day} {videos} в день: в {_join(times)}"
    if rules.lead_minutes:
        plan += f" — за {rules.lead_minutes} мин до пика, чтобы ролик успел набрать первые показы"
    lines.append(plan + ".")
    if slots:
        lines.append(
            "Ближайшие свободные слоты: " + " · ".join(when(slot, tz, now=now) for slot in slots)
        )
    return "\n".join(lines)


def format_queue(
    items: Sequence[Row],
    tz: tzinfo,
    *,
    now: datetime,
    names: Mapping[str, str] | None = None,
) -> str:
    if not items:
        return (
            "Очередь пуста. Добавить ролик: "
            'python -m tiktok_manager add видео.mp4 --caption "текст #хештег"'
        )
    headers = ["№", "Когда", "Статус", "Файл", "Подпись"]
    if names:
        headers.insert(1, "Аккаунт")
    rows = []
    notes = []
    for item in items:
        status = STATUS_LABELS.get(item["status"], item["status"])
        if item["status"] == store.SCHEDULED and item["auto_slot"]:
            status += " (пик)"
        row = [
            f"#{item['id']}",
            when(from_iso(item["publish_at"]), tz, now=now),
            status,
            shorten(Path(item["path"]).name, 30),
            shorten(item["caption"], 40) or "—",
        ]
        if names:
            row.insert(1, names.get(item["open_id"], item["open_id"]))
        rows.append(row)
        if item["note"]:
            notes.append(f"#{item['id']}: {item['note']}")
    text = table(headers, rows)
    if notes:
        text += "\n\n" + "\n".join(notes)
    return text
