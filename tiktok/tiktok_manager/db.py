"""Хранилище на SQLite: аккаунты, снимки статистики и очередь публикаций."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .api import Tokens

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    open_id            TEXT PRIMARY KEY,
    display_name       TEXT NOT NULL DEFAULT '',
    access_token       TEXT NOT NULL,
    access_expires_at  TEXT NOT NULL,
    refresh_token      TEXT NOT NULL,
    refresh_expires_at TEXT NOT NULL,
    scope              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_stats (
    open_id         TEXT    NOT NULL REFERENCES accounts(open_id) ON DELETE CASCADE,
    taken_at        TEXT    NOT NULL,
    follower_count  INTEGER NOT NULL,
    following_count INTEGER NOT NULL,
    likes_count     INTEGER NOT NULL,
    video_count     INTEGER NOT NULL,
    PRIMARY KEY (open_id, taken_at)
);

CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    open_id       TEXT    NOT NULL REFERENCES accounts(open_id) ON DELETE CASCADE,
    title         TEXT    NOT NULL DEFAULT '',
    share_url     TEXT    NOT NULL DEFAULT '',
    duration      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    view_count    INTEGER NOT NULL DEFAULT 0,
    like_count    INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    share_count   INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS video_stats (
    video_id      TEXT    NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    taken_at      TEXT    NOT NULL,
    view_count    INTEGER NOT NULL,
    like_count    INTEGER NOT NULL,
    comment_count INTEGER NOT NULL,
    share_count   INTEGER NOT NULL,
    PRIMARY KEY (video_id, taken_at)
);

CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    open_id       TEXT    NOT NULL REFERENCES accounts(open_id) ON DELETE CASCADE,
    path          TEXT    NOT NULL,
    mode          TEXT    NOT NULL,
    caption       TEXT    NOT NULL DEFAULT '',
    privacy_level TEXT    NOT NULL DEFAULT '',
    allow_comment INTEGER NOT NULL DEFAULT 0,
    allow_duet    INTEGER NOT NULL DEFAULT 0,
    allow_stitch  INTEGER NOT NULL DEFAULT 0,
    brand_organic INTEGER NOT NULL DEFAULT 0,
    brand_content INTEGER NOT NULL DEFAULT 0,
    is_aigc       INTEGER NOT NULL DEFAULT 0,
    publish_at    TEXT    NOT NULL,
    auto_slot     INTEGER NOT NULL DEFAULT 1,
    status        TEXT    NOT NULL DEFAULT 'scheduled',
    publish_id    TEXT    NOT NULL DEFAULT '',
    started_at    TEXT,
    post_id       TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_account ON videos(open_id, created_at);
CREATE INDEX IF NOT EXISTS idx_queue_status   ON queue(open_id, status, publish_at);
"""

# Статусы публикации в очереди.
SCHEDULED = "scheduled"  # ждёт своего времени
UPLOADING = "uploading"  # отправляется в TikTok
PROCESSING = "processing"  # файл загружен, TikTok его обрабатывает
PUBLISHED = "published"  # опубликован
INBOX_SENT = "inbox"  # черновик во «Входящих» TikTok, осталось нажать «Опубликовать»
FAILED = "failed"

IN_FLIGHT = (UPLOADING, PROCESSING)
# Публикации, с которыми сверяются интервал между роликами и лимит на день.
SLOT_TAKING = (SCHEDULED, UPLOADING, PROCESSING, PUBLISHED, INBOX_SENT)


@dataclass(frozen=True)
class PostSettings:
    """Что пользователь выбрал для ролика. По правилам TikTok всё выключено по умолчанию."""

    caption: str = ""
    privacy_level: str = ""
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    brand_organic: bool = False  # продвигает собственный бизнес автора
    brand_content: bool = False  # оплаченная реклама чужого бренда
    is_aigc: bool = False  # ролик создан ИИ


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _count(data: Mapping[str, Any], key: str) -> int:
    try:
        return int(data.get(key) or 0)
    except (TypeError, ValueError):
        return 0


class Database:
    def __init__(self, path: Path | str) -> None:
        try:
            # Автокоммит: каждая запись сразу на диске. Упади процесс посреди
            # загрузки ролика — publish_id уже сохранён, и судьбу публикации
            # можно будет узнать у TikTok, а не гадать.
            self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        except sqlite3.Error as exc:
            raise RuntimeError(f"Не удалось открыть базу {path}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA foreign_keys = ON")
            # WAL — чтобы команды в консоли читали базу, пока работает планировщик.
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript(SCHEMA)
        except sqlite3.Error as exc:
            self.conn.close()
            raise RuntimeError(f"Не удалось открыть базу {path}: {exc}") from exc

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --------------------------------------------------------------- accounts

    def save_tokens(self, tokens: Tokens) -> None:
        """Сохраняет токены: при первом входе создаёт аккаунт, потом обновляет."""
        now = to_iso(utcnow())
        self.conn.execute(
            """
            INSERT INTO accounts (open_id, access_token, access_expires_at, refresh_token,
                                  refresh_expires_at, scope, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(open_id) DO UPDATE SET
                access_token       = excluded.access_token,
                access_expires_at  = excluded.access_expires_at,
                refresh_token      = excluded.refresh_token,
                refresh_expires_at = excluded.refresh_expires_at,
                scope              = excluded.scope,
                updated_at         = excluded.updated_at
            """,
            (
                tokens.open_id,
                tokens.access_token,
                to_iso(tokens.access_expires_at),
                tokens.refresh_token,
                to_iso(tokens.refresh_expires_at),
                tokens.scope,
                now,
                now,
            ),
        )

    def set_display_name(self, open_id: str, name: str) -> None:
        self.conn.execute("UPDATE accounts SET display_name = ? WHERE open_id = ?", (name, open_id))

    def account(self, open_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM accounts WHERE open_id = ?", (open_id,)).fetchone()

    def accounts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()

    # ------------------------------------------------------------- statistics

    def add_snapshot(
        self,
        open_id: str,
        taken_at: datetime,
        user: Mapping[str, Any],
        videos: Sequence[Mapping[str, Any]],
    ) -> None:
        """Сохраняет счётчики аккаунта и всех его роликов на момент taken_at."""
        stamp = to_iso(taken_at)
        with self._transaction():
            # Без права user.info.stats счётчиков в ответе нет, а нули в истории
            # выглядели бы как обвал подписчиков.
            if "follower_count" in user:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO account_stats
                           (open_id, taken_at, follower_count, following_count,
                            likes_count, video_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        open_id,
                        stamp,
                        _count(user, "follower_count"),
                        _count(user, "following_count"),
                        _count(user, "likes_count"),
                        _count(user, "video_count"),
                    ),
                )
            for video in videos:
                video_id = str(video["id"])
                counts = (
                    _count(video, "view_count"),
                    _count(video, "like_count"),
                    _count(video, "comment_count"),
                    _count(video, "share_count"),
                )
                created = datetime.fromtimestamp(_count(video, "create_time"), timezone.utc)
                title = str(video.get("title") or video.get("video_description") or "")
                self.conn.execute(
                    """
                    INSERT INTO videos (id, open_id, title, share_url, duration, created_at,
                                        view_count, like_count, comment_count, share_count,
                                        updated_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title         = excluded.title,
                        share_url     = excluded.share_url,
                        duration      = excluded.duration,
                        view_count    = excluded.view_count,
                        like_count    = excluded.like_count,
                        comment_count = excluded.comment_count,
                        share_count   = excluded.share_count,
                        updated_at    = excluded.updated_at
                    """,
                    (
                        video_id,
                        open_id,
                        title.strip(),
                        str(video.get("share_url") or ""),
                        _count(video, "duration"),
                        to_iso(created),
                        *counts,
                        stamp,
                    ),
                )
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO video_stats
                           (video_id, taken_at, view_count, like_count, comment_count,
                            share_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (video_id, stamp, *counts),
                )

    def last_snapshot_at(self, open_id: str) -> datetime | None:
        row = self.conn.execute(
            """
            SELECT MAX(taken_at) FROM (
                SELECT taken_at FROM account_stats WHERE open_id = ?
                UNION ALL
                SELECT s.taken_at
                  FROM video_stats s JOIN videos v ON v.id = s.video_id
                 WHERE v.open_id = ?
            )
            """,
            (open_id, open_id),
        ).fetchone()
        return from_iso(row[0]) if row and row[0] else None

    def latest_account_stats(self, open_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM account_stats WHERE open_id = ? ORDER BY taken_at DESC LIMIT 1",
            (open_id,),
        ).fetchone()

    def account_stats_at(self, open_id: str, moment: datetime) -> sqlite3.Row | None:
        """Последний снимок не позже moment, а если таких нет — самый ранний."""
        row = self.conn.execute(
            """
            SELECT * FROM account_stats
             WHERE open_id = ? AND taken_at <= ?
             ORDER BY taken_at DESC LIMIT 1
            """,
            (open_id, to_iso(moment)),
        ).fetchone()
        if row is not None:
            return row
        return self.conn.execute(
            "SELECT * FROM account_stats WHERE open_id = ? ORDER BY taken_at LIMIT 1",
            (open_id,),
        ).fetchone()

    def videos(self, open_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM videos WHERE open_id = ? ORDER BY created_at DESC", (open_id,)
        ).fetchall()

    def video_points(self, open_id: str, since: datetime) -> list[sqlite3.Row]:
        """Снимки просмотров роликов начиная с since — сырьё для пиковых часов."""
        return self.conn.execute(
            """
            SELECT s.video_id, s.taken_at, s.view_count, v.created_at
              FROM video_stats s
              JOIN videos v ON v.id = s.video_id
             WHERE v.open_id = ? AND s.taken_at >= ?
             ORDER BY s.video_id, s.taken_at
            """,
            (open_id, to_iso(since)),
        ).fetchall()

    # ------------------------------------------------------------------ queue

    def add_item(
        self,
        *,
        open_id: str,
        path: str,
        mode: str,
        settings: PostSettings,
        publish_at: datetime,
        auto_slot: bool,
    ) -> int:
        now = to_iso(utcnow())
        cursor = self.conn.execute(
            """
            INSERT INTO queue (open_id, path, mode, caption, privacy_level, allow_comment,
                               allow_duet, allow_stitch, brand_organic, brand_content,
                               is_aigc, publish_at, auto_slot, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                open_id,
                path,
                mode,
                settings.caption,
                settings.privacy_level,
                int(settings.allow_comment),
                int(settings.allow_duet),
                int(settings.allow_stitch),
                int(settings.brand_organic),
                int(settings.brand_content),
                int(settings.is_aigc),
                to_iso(publish_at),
                int(auto_slot),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid or 0)

    def item(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()

    def items(self, open_id: str | None = None) -> list[sqlite3.Row]:
        if open_id is None:
            return self.conn.execute("SELECT * FROM queue ORDER BY publish_at, id").fetchall()
        return self.conn.execute(
            "SELECT * FROM queue WHERE open_id = ? ORDER BY publish_at, id", (open_id,)
        ).fetchall()

    def due(self, open_id: str, now: datetime) -> list[sqlite3.Row]:
        """Ролики, время публикации которых наступило."""
        return self.conn.execute(
            """
            SELECT * FROM queue
             WHERE open_id = ? AND status = ? AND publish_at <= ?
             ORDER BY publish_at, id
            """,
            (open_id, SCHEDULED, to_iso(now)),
        ).fetchall()

    def in_flight(self, open_id: str) -> list[sqlite3.Row]:
        """Ролики, уже отправленные в TikTok, но без итогового статуса."""
        return self.conn.execute(
            f"""
            SELECT * FROM queue
             WHERE open_id = ? AND status IN ({_marks(IN_FLIGHT)})
             ORDER BY publish_at, id
            """,
            (open_id, *IN_FLIGHT),
        ).fetchall()

    def taken_slots(
        self, open_id: str, since: datetime, *, exclude_id: int | None = None
    ) -> list[datetime]:
        rows = self.conn.execute(
            f"""
            SELECT publish_at FROM queue
             WHERE open_id = ? AND status IN ({_marks(SLOT_TAKING)})
               AND publish_at >= ? AND id != ?
            """,
            (open_id, *SLOT_TAKING, to_iso(since), -1 if exclude_id is None else exclude_id),
        ).fetchall()
        return [from_iso(row["publish_at"]) for row in rows]

    def claim(self, item_id: int, now: datetime) -> bool:
        """Забирает ролик в работу. False — его уже забрал другой процесс."""
        cursor = self.conn.execute(
            "UPDATE queue SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (UPLOADING, to_iso(now), item_id, SCHEDULED),
        )
        return cursor.rowcount == 1

    def unclaim(self, item_id: int, now: datetime) -> None:
        """Возвращает в очередь ролик, который так и не ушёл в TikTok."""
        self.conn.execute(
            """
            UPDATE queue SET status = ?, publish_id = '', started_at = NULL, updated_at = ?
             WHERE id = ? AND status = ?
            """,
            (SCHEDULED, to_iso(now), item_id, UPLOADING),
        )

    def set_publish_id(self, item_id: int, publish_id: str, now: datetime) -> None:
        stamp = to_iso(now)
        self.conn.execute(
            "UPDATE queue SET publish_id = ?, started_at = ?, updated_at = ? WHERE id = ?",
            (publish_id, stamp, stamp, item_id),
        )

    def set_status(
        self, item_id: int, status: str, now: datetime, *, post_id: str = "", note: str = ""
    ) -> None:
        self.conn.execute(
            "UPDATE queue SET status = ?, post_id = ?, note = ?, updated_at = ? WHERE id = ?",
            (status, post_id, note, to_iso(now), item_id),
        )

    def fail(self, item_id: int, note: str, now: datetime) -> None:
        self.set_status(item_id, FAILED, now, note=note)

    def reschedule(
        self, item_id: int, publish_at: datetime, now: datetime, *, attempts: int, note: str = ""
    ) -> None:
        self.conn.execute(
            """
            UPDATE queue
               SET status = ?, publish_at = ?, publish_id = '', started_at = NULL,
                   attempts = ?, note = ?, updated_at = ?
             WHERE id = ?
            """,
            (SCHEDULED, to_iso(publish_at), attempts, note, to_iso(now), item_id),
        )

    def delete_item(self, item_id: int) -> bool:
        cursor = self.conn.execute("DELETE FROM queue WHERE id = ?", (item_id,))
        return cursor.rowcount == 1


def _marks(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)
