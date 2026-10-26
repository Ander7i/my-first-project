"""Фоновый режим: снимает статистику по расписанию и публикует очередь."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from sqlite3 import Row
from typing import Any

from .api import LoginRequired, TikTokClient, TikTokError
from .config import Config
from .db import Database, from_iso, utcnow
from .planner import find_slot
from .publisher import check_status, publish
from .slots import NoFreeSlot

log = logging.getLogger(__name__)

TICK_SECONDS = 30
# Опоздали к слоту сильнее этого (компьютер спал, планировщик не работал) —
# переносим ролик на следующий пик, а не публикуем в неудачное время.
LATE_GRACE = timedelta(minutes=30)
# Не получилось снять статистику — пробуем снова через столько.
COLLECT_RETRY = timedelta(minutes=5)
# Если без нового входа к аккаунту не попасть, не трогаем его час, чтобы не
# засорять лог и не дёргать TikTok каждые полминуты.
LOGIN_PAUSE = timedelta(hours=1)


def account_label(account: Row) -> str:
    return account["display_name"] or account["open_id"]


def collect(
    client: TikTokClient, db: Database, open_id: str, now: datetime
) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
    """Снимает текущие счётчики аккаунта и всех его публичных роликов."""
    user = client.user_info(open_id)
    videos = client.videos(open_id)
    db.add_snapshot(open_id, now, user, videos)
    if user.get("display_name"):
        db.set_display_name(open_id, str(user["display_name"]))
    return user, videos


class Runner:
    def __init__(
        self,
        config: Config,
        db: Database,
        client: TikTokClient,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.db = db
        self.client = client
        self.clock = clock
        self._next_collect: dict[str, datetime] = {}
        self._paused_until: dict[str, datetime] = {}

    def run_forever(self, *, sleep: Callable[[float], None] = time.sleep) -> None:
        log.info(
            "Планировщик запущен: статистика раз в %s мин, режим публикации %s",
            self.config.collect_every_minutes,
            self.config.publish_mode,
        )
        while True:
            self.tick()
            sleep(TICK_SECONDS)

    def tick(self) -> None:
        now = self.clock()
        for account in self.db.accounts():
            open_id = account["open_id"]
            if self._paused_until.get(open_id, now) > now:
                continue
            try:
                self._tick_account(open_id, now)
            except LoginRequired as exc:
                log.error("%s: %s", account_label(account), exc)
                self._paused_until[open_id] = now + LOGIN_PAUSE
            except Exception:
                # Сбой одного аккаунта не должен останавливать остальные.
                log.exception("%s: непредвиденная ошибка", account_label(account))

    def _tick_account(self, open_id: str, now: datetime) -> None:
        if now >= self._next_collect.get(open_id, now):
            self._collect(open_id, now)

        for item in self.db.in_flight(open_id):
            check_status(item, client=self.client, db=self.db, config=self.config, now=now)

        for item in self.db.due(open_id, now):
            if now - from_iso(item["publish_at"]) > LATE_GRACE:
                self._move_missed(item, now)
                continue
            publish(item, client=self.client, db=self.db, now=now)

    def _collect(self, open_id: str, now: datetime) -> None:
        try:
            user, videos = collect(self.client, self.db, open_id, now)
        except LoginRequired:
            raise
        except TikTokError as exc:
            log.warning("Статистика не снята: %s", exc)
            self._next_collect[open_id] = now + COLLECT_RETRY
            return
        self._next_collect[open_id] = now + timedelta(minutes=self.config.collect_every_minutes)
        log.info(
            "%s: статистика снята — подписчиков %s, публичных роликов %s",
            user.get("display_name") or open_id,
            user.get("follower_count", "?"),
            len(videos),
        )

    def _move_missed(self, item: Row, now: datetime) -> None:
        try:
            slot, _ = find_slot(self.config, self.db, item["open_id"], now, exclude_id=item["id"])
        except NoFreeSlot as exc:
            self.db.fail(item["id"], f"Время публикации пропущено. {exc}", now)
            log.error("#%s: время публикации пропущено. %s", item["id"], exc)
            return
        self.db.reschedule(
            item["id"],
            slot,
            now,
            attempts=item["attempts"],
            note="Время публикации было пропущено — перенесено на ближайший пик.",
        )
        log.warning(
            "#%s: время публикации пропущено, перенёс на %s",
            item["id"],
            slot.astimezone(self.config.timezone).strftime("%d.%m %H:%M"),
        )
