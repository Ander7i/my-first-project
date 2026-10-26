"""Загрузка настроек из .env и переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Режимы публикации.
DIRECT = "direct"  # ролик публикуется сразу
INBOX = "inbox"  # ролик ложится черновиком во «Входящие» TikTok, публикуете вы
PUBLISH_MODES = (DIRECT, INBOX)

DEFAULT_REDIRECT_URI = "http://localhost:3455/callback/"


@dataclass(frozen=True)
class Config:
    client_key: str
    client_secret: str
    redirect_uri: str
    publish_mode: str
    timezone: tzinfo
    peak_hours: tuple[int, ...]
    lead_minutes: int
    max_posts_per_day: int
    min_gap_hours: int
    collect_every_minutes: int
    db_path: Path

    def require_app(self) -> None:
        """Ключи приложения нужны только командам, которые ходят в TikTok."""
        if not self.client_key or not self.client_secret:
            raise RuntimeError(
                "Не заданы TIKTOK_CLIENT_KEY и TIKTOK_CLIENT_SECRET. Скопируйте .env.example "
                "в .env и впишите ключи со страницы приложения на developers.tiktok.com."
            )


def load_config() -> Config:
    # .env ищем только в текущей папке и выше не поднимаемся: в этом репозитории
    # уровнем выше лежит .env Telegram-бота, и его DB_PATH увёл бы статистику
    # в чужую базу.
    load_dotenv(Path.cwd() / ".env")

    publish_mode = os.getenv("PUBLISH_MODE", "").strip().lower() or INBOX
    if publish_mode not in PUBLISH_MODES:
        raise RuntimeError(
            f"PUBLISH_MODE должен быть direct или inbox, а получено: {publish_mode!r}."
        )

    return Config(
        client_key=os.getenv("TIKTOK_CLIENT_KEY", "").strip(),
        client_secret=os.getenv("TIKTOK_CLIENT_SECRET", "").strip(),
        redirect_uri=_redirect_uri(os.getenv("TIKTOK_REDIRECT_URI", "").strip()),
        publish_mode=publish_mode,
        timezone=_timezone(os.getenv("TIMEZONE", "").strip()),
        peak_hours=_parse_hours(os.getenv("PEAK_HOURS", "")),
        lead_minutes=_int("LEAD_MINUTES", 60, minimum=0, maximum=720),
        # Больше 15 в сутки TikTok через API всё равно не пропустит.
        max_posts_per_day=_int("MAX_POSTS_PER_DAY", 2, minimum=1, maximum=15),
        min_gap_hours=_int("MIN_GAP_HOURS", 3, minimum=0, maximum=23),
        # Реже чем раз в два часа пиковые часы уже не посчитать: прирост
        # просмотров за длинный промежуток не разложить по часам.
        collect_every_minutes=_int("COLLECT_EVERY_MINUTES", 60, minimum=10, maximum=120),
        db_path=Path(os.getenv("DB_PATH", "").strip() or "tiktok.db"),
    )


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} должен быть целым числом, а получено: {raw!r}.") from None
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} должен быть от {minimum} до {maximum}, а получено: {value}.")
    return value


def _parse_hours(raw: str) -> tuple[int, ...]:
    """Часы через запятую: «19,20,13» или «19:00, 13:00», как в TikTok Studio."""
    hours: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        hour_part, _, minutes = chunk.partition(":")
        try:
            hour = int(hour_part)
            if minutes and int(minutes) != 0:
                raise ValueError
        except ValueError:
            raise RuntimeError(
                f"PEAK_HOURS — это целые часы через запятую, например 19,20,13, "
                f"а получено: {chunk!r}."
            ) from None
        if not 0 <= hour <= 23:
            raise RuntimeError(f"В PEAK_HOURS час должен быть от 0 до 23, а получено: {hour}.")
        if hour not in hours:
            hours.append(hour)
    return tuple(hours)


def _timezone(raw: str) -> tzinfo:
    if not raw:
        # Пояс компьютера. Переходы на летнее время он не знает, поэтому
        # лучше задать TIMEZONE явно.
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        raise RuntimeError(
            f"Неизвестный часовой пояс TIMEZONE={raw!r}. Примеры: Europe/Moscow, "
            "Asia/Almaty, Europe/Minsk. На Windows поясам нужен пакет tzdata: "
            "pip install -r requirements.txt"
        ) from None


def _redirect_uri(raw: str) -> str:
    value = raw or DEFAULT_REDIRECT_URI
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        port = None
    if (
        parts.scheme != "http"
        or parts.hostname not in ("localhost", "127.0.0.1")
        or port is None
        or parts.query
        or parts.fragment
    ):
        raise RuntimeError(
            f"TIKTOK_REDIRECT_URI={value!r} не подходит. Нужен адрес на localhost с портом, "
            f"например {DEFAULT_REDIRECT_URI} — и ровно такой же адрес должен стоять в "
            "настройках Login Kit (Desktop) на developers.tiktok.com."
        )
    return value
