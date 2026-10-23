"""Слой доступа к SQLite."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id      INTEGER PRIMARY KEY,
    username   TEXT,
    full_name  TEXT,
    city       TEXT,
    city_key   TEXT NOT NULL DEFAULT '',
    age        INTEGER,
    has_car    INTEGER NOT NULL DEFAULT 0,
    contact    TEXT,
    categories TEXT NOT NULL DEFAULT '',
    consent_at TEXT,
    status     TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    city        TEXT    NOT NULL,
    payment     INTEGER NOT NULL,
    deadline    TEXT    NOT NULL,
    slots       INTEGER NOT NULL DEFAULT 1,
    description TEXT    NOT NULL DEFAULT '',
    checklist   TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'open',
    created_by  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    status       TEXT    NOT NULL DEFAULT 'claimed',
    note         TEXT    NOT NULL DEFAULT '',
    claimed_at   TEXT    NOT NULL,
    submitted_at TEXT,
    decided_at   TEXT,
    paid_at      TEXT,
    UNIQUE (task_id, user_id)
);

CREATE TABLE IF NOT EXISTS report_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL,
    file_id       TEXT,
    text          TEXT,
    position      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_assignments_user ON assignments(user_id, status);
CREATE INDEX IF NOT EXISTS idx_assignments_task ON assignments(task_id, status);
CREATE INDEX IF NOT EXISTS idx_report_items    ON report_items(assignment_id, position);
CREATE INDEX IF NOT EXISTS idx_users_city      ON users(city_key, status);
"""

# Статусы заявки на задание.
CLAIMED = "claimed"
SUBMITTED = "submitted"
ACCEPTED = "accepted"
REWORK = "rework"
PAID = "paid"
CANCELLED = "cancelled"

# Статусы, при которых слот задания считается занятым.
ACTIVE_ASSIGNMENT_STATUSES = (CLAIMED, SUBMITTED, ACCEPTED, REWORK, PAID)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_city(value: str) -> str:
    """Приводит название города к виду для сравнения.

    В SQLite lower() работает только с латиницей, поэтому «Москва» и «москва»
    там не совпадут. Нормализуем на стороне Python и храним результат
    отдельной колонкой city_key.
    """
    return " ".join((value or "").strip().lower().replace("ё", "е").split())


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        # Соединение одно на всё приложение, поэтому выдачу слотов
        # сериализуем явно — иначе две корутины займут один слот.
        self._claim_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База не подключена: вызовите connect()")
        return self._conn

    async def connect(self) -> None:
        # isolation_level=None — режим автокоммита: sqlite3 не открывает
        # транзакции сам, поэтому явный BEGIN IMMEDIATE в claim_task работает.
        self._conn = await aiosqlite.connect(self._path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ users

    async def touch_user(self, tg_id: int, username: str | None, full_name: str) -> None:
        """Создаёт запись о пользователе при первом контакте, иначе обновляет имя."""
        await self.conn.execute(
            """
            INSERT INTO users (tg_id, username, full_name, created_at)
                 VALUES (?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username,
                                             full_name = excluded.full_name
            """,
            (tg_id, username, full_name, _now()),
        )
        await self.conn.commit()

    async def get_user(self, tg_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cur:
            return await cur.fetchone()

    async def set_consent(self, tg_id: int) -> None:
        await self.conn.execute(
            "UPDATE users SET consent_at = ? WHERE tg_id = ?", (_now(), tg_id)
        )
        await self.conn.commit()

    async def save_profile(
        self,
        tg_id: int,
        *,
        city: str,
        age: int,
        has_car: bool,
        contact: str,
        categories: list[str],
    ) -> None:
        await self.conn.execute(
            """
            UPDATE users
               SET city = ?, city_key = ?, age = ?, has_car = ?, contact = ?,
                   categories = ?, status = 'active'
             WHERE tg_id = ?
            """,
            (
                city.strip(),
                normalize_city(city),
                age,
                int(has_car),
                contact,
                ",".join(categories),
                tg_id,
            ),
        )
        await self.conn.commit()

    async def set_user_status(self, tg_id: int, status: str) -> None:
        await self.conn.execute("UPDATE users SET status = ? WHERE tg_id = ?", (status, tg_id))
        await self.conn.commit()

    async def delete_user(self, tg_id: int) -> None:
        """Полностью удаляет данные пользователя (по команде /stop)."""
        await self.conn.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
        await self.conn.commit()

    async def match_users(self, city: str, category: str) -> list[aiosqlite.Row]:
        """Активные исполнители нужного города с подходящей категорией."""
        async with self.conn.execute(
            """
            SELECT * FROM users
             WHERE status = 'active'
               AND consent_at IS NOT NULL
               AND city_key = ?
               AND instr(',' || categories || ',', ',' || ? || ',') > 0
            """,
            (normalize_city(city), category),
        ) as cur:
            return list(await cur.fetchall())

    async def stats(self) -> dict[str, int]:
        queries = {
            "users_active": "SELECT COUNT(*) FROM users WHERE status = 'active'",
            "users_total": "SELECT COUNT(*) FROM users",
            "tasks_open": "SELECT COUNT(*) FROM tasks WHERE status = 'open'",
            "reports_pending": f"SELECT COUNT(*) FROM assignments WHERE status = '{SUBMITTED}'",
            "awaiting_payout": f"SELECT COUNT(*) FROM assignments WHERE status = '{ACCEPTED}'",
        }
        result = {}
        for key, sql in queries.items():
            async with self.conn.execute(sql) as cur:
                row = await cur.fetchone()
                result[key] = row[0] if row else 0
        return result

    # ------------------------------------------------------------------ tasks

    async def create_task(self, data: dict[str, Any], created_by: int) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO tasks (title, category, city, payment, deadline, slots,
                               description, checklist, created_by, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["title"],
                data["category"],
                data["city"],
                data["payment"],
                data["deadline"],
                data["slots"],
                data.get("description", ""),
                data.get("checklist", ""),
                created_by,
                _now(),
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def get_task(self, task_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
            return await cur.fetchone()

    async def list_tasks(self, status: str | None = None, limit: int = 20) -> list[aiosqlite.Row]:
        if status:
            sql = "SELECT * FROM tasks WHERE status = ? ORDER BY id DESC LIMIT ?"
            params: tuple[Any, ...] = (status, limit)
        else:
            sql = "SELECT * FROM tasks ORDER BY id DESC LIMIT ?"
            params = (limit,)
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def set_task_status(self, task_id: int, status: str) -> None:
        await self.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        await self.conn.commit()

    async def taken_slots(self, task_id: int) -> int:
        placeholders = ",".join("?" * len(ACTIVE_ASSIGNMENT_STATUSES))
        async with self.conn.execute(
            f"SELECT COUNT(*) FROM assignments WHERE task_id = ? AND status IN ({placeholders})",
            (task_id, *ACTIVE_ASSIGNMENT_STATUSES),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    # ------------------------------------------------------------ assignments

    async def claim_task(self, task_id: int, user_id: int) -> tuple[int | None, str]:
        """Пытается занять слот задания.

        Возвращает (id заявки, код результата). Коды: ok, no_task, closed,
        no_slots, already.
        """
        # Блокировка защищает от гонки внутри процесса, BEGIN IMMEDIATE —
        # от второго процесса, работающего с тем же файлом базы.
        async with self._claim_lock:
            return await self._claim_task_locked(task_id, user_id)

    async def _claim_task_locked(self, task_id: int, user_id: int) -> tuple[int | None, str]:
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.conn.execute(
                "SELECT slots, status FROM tasks WHERE id = ?", (task_id,)
            ) as cur:
                task = await cur.fetchone()
            if task is None:
                await self.conn.rollback()
                return None, "no_task"
            if task["status"] != "open":
                await self.conn.rollback()
                return None, "closed"

            async with self.conn.execute(
                "SELECT id, status FROM assignments WHERE task_id = ? AND user_id = ?",
                (task_id, user_id),
            ) as cur:
                mine = await cur.fetchone()
            if mine is not None and mine["status"] != CANCELLED:
                await self.conn.rollback()
                return mine["id"], "already"

            placeholders = ",".join("?" * len(ACTIVE_ASSIGNMENT_STATUSES))
            async with self.conn.execute(
                f"SELECT COUNT(*) FROM assignments "
                f"WHERE task_id = ? AND status IN ({placeholders})",
                (task_id, *ACTIVE_ASSIGNMENT_STATUSES),
            ) as cur:
                row = await cur.fetchone()
                taken = row[0] if row else 0
            if taken >= task["slots"]:
                await self.conn.rollback()
                return None, "no_slots"

            if mine is not None:
                # Ранее отменённая заявка — переиспользуем её.
                await self.conn.execute(
                    "UPDATE assignments SET status = ?, claimed_at = ?, note = '' WHERE id = ?",
                    (CLAIMED, _now(), mine["id"]),
                )
                assignment_id = mine["id"]
            else:
                cur = await self.conn.execute(
                    "INSERT INTO assignments (task_id, user_id, claimed_at) VALUES (?, ?, ?)",
                    (task_id, user_id, _now()),
                )
                assignment_id = int(cur.lastrowid)

            await self.conn.commit()
            return assignment_id, "ok"
        except Exception:
            await self.conn.rollback()
            raise

    async def get_assignment(self, assignment_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            """
            SELECT a.*, t.title, t.category, t.city, t.payment, t.deadline, t.checklist,
                   u.username, u.full_name, u.contact
              FROM assignments a
              JOIN tasks t ON t.id = a.task_id
              JOIN users u ON u.tg_id = a.user_id
             WHERE a.id = ?
            """,
            (assignment_id,),
        ) as cur:
            return await cur.fetchone()

    async def list_user_assignments(
        self, user_id: int, statuses: tuple[str, ...]
    ) -> list[aiosqlite.Row]:
        placeholders = ",".join("?" * len(statuses))
        async with self.conn.execute(
            f"""
            SELECT a.*, t.title, t.city, t.payment, t.deadline
              FROM assignments a
              JOIN tasks t ON t.id = a.task_id
             WHERE a.user_id = ? AND a.status IN ({placeholders})
             ORDER BY a.id DESC
            """,
            (user_id, *statuses),
        ) as cur:
            return list(await cur.fetchall())

    async def list_assignments_by_status(
        self, status: str, limit: int = 20
    ) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT a.*, t.title, t.city, t.payment, u.username, u.full_name
              FROM assignments a
              JOIN tasks t ON t.id = a.task_id
              JOIN users u ON u.tg_id = a.user_id
             WHERE a.status = ?
             ORDER BY a.id ASC
             LIMIT ?
            """,
            (status, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def set_assignment_status(
        self, assignment_id: int, status: str, note: str | None = None
    ) -> None:
        stamps = {
            SUBMITTED: "submitted_at",
            ACCEPTED: "decided_at",
            REWORK: "decided_at",
            PAID: "paid_at",
        }
        sets = ["status = ?"]
        params: list[Any] = [status]
        if note is not None:
            sets.append("note = ?")
            params.append(note)
        if status in stamps:
            sets.append(f"{stamps[status]} = ?")
            params.append(_now())
        params.append(assignment_id)
        await self.conn.execute(
            f"UPDATE assignments SET {', '.join(sets)} WHERE id = ?", params
        )
        await self.conn.commit()

    # ----------------------------------------------------------- report items

    async def clear_report(self, assignment_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM report_items WHERE assignment_id = ?", (assignment_id,)
        )
        await self.conn.commit()

    async def add_report_item(
        self,
        assignment_id: int,
        kind: str,
        *,
        file_id: str | None = None,
        text: str | None = None,
        position: int = 0,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO report_items (assignment_id, kind, file_id, text, position)
                 VALUES (?, ?, ?, ?, ?)
            """,
            (assignment_id, kind, file_id, text, position),
        )
        await self.conn.commit()

    async def list_report_items(self, assignment_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM report_items WHERE assignment_id = ? ORDER BY position, id",
            (assignment_id,),
        ) as cur:
            return list(await cur.fetchall())
