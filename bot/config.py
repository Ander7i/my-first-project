"""Загрузка настроек из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_CATEGORIES = (
    "Магазины",
    "Кафе и рестораны",
    "АЗС",
    "Аптеки",
    "Банки",
    "Телефонные звонки",
)


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: tuple[int, ...]
    group_chat_id: int | None
    db_path: Path
    categories: tuple[str, ...]
    proxy_url: str | None

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def _parse_ids(raw: str) -> tuple[int, ...]:
    ids = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            raise RuntimeError(
                f"ADMIN_IDS должен быть числом или числами через запятую, "
                f"а получено: {chunk!r}. Свой ID можно узнать командой /myid."
            ) from None
    return tuple(ids)


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен от @BotFather."
        )

    admin_ids = _parse_ids(os.getenv("ADMIN_IDS", ""))
    if not admin_ids:
        raise RuntimeError(
            "Не задан ADMIN_IDS. Запустите бота, напишите ему /myid и впишите полученный ID в .env."
        )

    raw_group = os.getenv("GROUP_CHAT_ID", "").strip()
    group_chat_id = int(raw_group) if raw_group else None

    raw_categories = os.getenv("CATEGORIES", "").strip()
    categories = (
        tuple(c.strip() for c in raw_categories.split(",") if c.strip())
        if raw_categories
        else DEFAULT_CATEGORIES
    )

    return Config(
        token=token,
        admin_ids=admin_ids,
        group_chat_id=group_chat_id,
        db_path=Path(os.getenv("DB_PATH", "shopper.db")),
        categories=categories,
        proxy_url=_find_proxy(),
    )


def _find_proxy() -> str | None:
    """Прокси для доступа к api.telegram.org, если напрямую он недоступен.

    Берём PROXY_URL, иначе — стандартные переменные окружения. aiogram,
    в отличие от curl, сам их не читает.
    """
    for name in ("PROXY_URL", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None
