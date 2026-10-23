"""Слой доступа к SQLite."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .catalog import check_by_code

TABLES = """
CREATE TABLE IF NOT EXISTS users (
    tg_id      INTEGER PRIMARY KEY,
    username   TEXT,
    full_name  TEXT,
    city       TEXT,
    city_key   TEXT    NOT NULL DEFAULT '',
    age        INTEGER,
    has_car    INTEGER NOT NULL DEFAULT 0,
    has_ip     INTEGER NOT NULL DEFAULT 0,
    contact    TEXT,
    products   TEXT    NOT NULL DEFAULT '',
    consent_at TEXT,
    status     TEXT    NOT NULL DEFAULT 'new',
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    check_code TEXT    NOT NULL,
    bank       TEXT    NOT NULL DEFAULT '',
    title      TEXT    NOT NULL,
    city       TEXT    NOT NULL DEFAULT '',
    city_key   TEXT    NOT NULL DEFAULT '',
    remote     INTEGER NOT NULL DEFAULT 0,
    payment    INTEGER NOT NULL,
    deadline   TEXT    NOT NULL,
    due_date   TEXT    NOT NULL DEFAULT '',
    slots      INTEGER NOT NULL DEFAULT 1,
    notes      TEXT    NOT NULL DEFAULT '',
    link       TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'open',
    created_by INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    status       TEXT    NOT NULL DEFAULT 'claimed',
    note         TEXT    NOT NULL DEFAULT '',
    ack_at       TEXT,
    claimed_at   TEXT    NOT NULL,
    submitted_at TEXT,
    decided_at   TEXT,
    paid_at      TEXT,
    UNIQUE (task_id, user_id)
);

CREATE TABLE IF NOT EXISTS reminders (
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL,
    sent_at       TEXT    NOT NULL,
    PRIMARY KEY (assignment_id, kind)
);

CREATE TABLE IF NOT EXISTS report_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL,
    file_id       TEXT,
    text          TEXT,
    position      INTEGER NOT NULL DEFAULT 0
);
"""

# Индексы создаются ПОСЛЕ миграций: часть из них ссылается на колонки,
# которых в старой базе ещё нет.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_assignments_user ON assignments(user_id, status);
CREATE INDEX IF NOT EXISTS idx_assignments_task ON assignments(task_id, status);
CREATE INDEX IF NOT EXISTS idx_report_items    ON report_items(assignment_id, position);
CREATE INDEX IF NOT EXISTS idx_users_city      ON users(city_key, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status, check_code);
"""

# Колонки, добавленные после первого релиза. При старте доливаются в
# существующую базу, чтобы не терять уже собранные анкеты.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("users", "has_ip", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "products", "TEXT NOT NULL DEFAULT ''"),
    ("users", "city_key", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "check_code", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "bank", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "city_key", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "remote", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "notes", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "link", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "due_date", "TEXT NOT NULL DEFAULT ''"),
    ("assignments", "ack_at", "TEXT"),
)

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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        self._conn = conn
        try:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.executescript(TABLES)
            await self._apply_migrations()
            await conn.executescript(INDEXES)
        except Exception:
            # Рабочий поток aiosqlite не демонический: без закрытия ошибка
            # схемы превратится в вечное зависание вместо сообщения.
            self._conn = None
            await conn.close()
            raise

    async def _apply_migrations(self) -> None:
        """Доливает колонки, которых нет в уже существующей базе."""
        for table, column, definition in MIGRATIONS:
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row["name"] for row in await cur.fetchall()}
            if column not in existing:
                await self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

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

    async def get_user(self, tg_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cur:
            return await cur.fetchone()

    async def set_consent(self, tg_id: int) -> None:
        await self.conn.execute(
            "UPDATE users SET consent_at = ? WHERE tg_id = ?", (_now(), tg_id)
        )

    async def save_profile(
        self,
        tg_id: int,
        *,
        city: str,
        age: int,
        has_car: bool,
        has_ip: bool,
        contact: str,
        products: list[str],
    ) -> None:
        await self.conn.execute(
            """
            UPDATE users
               SET city = ?, city_key = ?, age = ?, has_car = ?, has_ip = ?,
                   contact = ?, products = ?, status = 'active'
             WHERE tg_id = ?
            """,
            (
                city.strip(),
                normalize_city(city),
                age,
                int(has_car),
                int(has_ip),
                contact,
                ",".join(products),
                tg_id,
            ),
        )

    async def set_user_status(self, tg_id: int, status: str) -> None:
        await self.conn.execute("UPDATE users SET status = ? WHERE tg_id = ?", (status, tg_id))

    async def set_has_ip(self, tg_id: int, has_ip: bool) -> None:
        """После задания на регистрацию ИП статус исполнителя меняется."""
        await self.conn.execute(
            "UPDATE users SET has_ip = ? WHERE tg_id = ?", (int(has_ip), tg_id)
        )

    async def delete_user(self, tg_id: int) -> None:
        """Полностью удаляет данные пользователя (по команде /stop)."""
        await self.conn.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))

    async def match_users(
        self,
        product: str,
        *,
        city: str = "",
        remote: bool = False,
        requires_ip: bool | None = None,
    ) -> list[aiosqlite.Row]:
        """Активные исполнители, подходящие под задание.

        Для дистанционных проверок город не важен. requires_ip отбирает по
        наличию действующего ИП: True — только у кого есть, False — только у
        кого нет, None — всем.
        """
        sql = [
            "SELECT * FROM users",
            "WHERE status = 'active'",
            "  AND consent_at IS NOT NULL",
            "  AND instr(',' || products || ',', ',' || ? || ',') > 0",
        ]
        params: list[Any] = [product]

        if not remote:
            sql.append("  AND city_key = ?")
            params.append(normalize_city(city))
        if requires_ip is not None:
            sql.append("  AND has_ip = ?")
            params.append(int(requires_ip))

        async with self.conn.execute("\n".join(sql), params) as cur:
            return list(await cur.fetchall())

    async def stats(self) -> dict[str, int]:
        queries = {
            "users_active": "SELECT COUNT(*) FROM users WHERE status = 'active'",
            "users_total": "SELECT COUNT(*) FROM users",
            "users_with_ip": "SELECT COUNT(*) FROM users WHERE status='active' AND has_ip=1",
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
        city = data.get("city", "")
        cur = await self.conn.execute(
            """
            INSERT INTO tasks (check_code, bank, title, city, city_key, remote,
                               payment, deadline, due_date, slots, notes, link,
                               created_by, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["check_code"],
                data.get("bank", ""),
                data["title"],
                city,
                normalize_city(city),
                int(data.get("remote", False)),
                data["payment"],
                data["deadline"],
                data.get("due_date", ""),
                data["slots"],
                data.get("notes", ""),
                data.get("link", ""),
                created_by,
                _now(),
            ),
        )
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

    async def taken_slots(self, task_id: int) -> int:
        placeholders = ",".join("?" * len(ACTIVE_ASSIGNMENT_STATUSES))
        async with self.conn.execute(
            f"SELECT COUNT(*) FROM assignments WHERE task_id = ? AND status IN ({placeholders})",
            (task_id, *ACTIVE_ASSIGNMENT_STATUSES),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def open_tasks_for_user(self, user: aiosqlite.Row) -> list[aiosqlite.Row]:
        """Открытые задания, подходящие исполнителю и ещё не взятые им.

        Фильтр по продукту и ИП делается в Python: признаки берутся из
        каталога по check_code, а не из базы.
        """
        async with self.conn.execute(
            """
            SELECT t.* FROM tasks t
             WHERE t.status = 'open'
               AND NOT EXISTS (
                     SELECT 1 FROM assignments a
                      WHERE a.task_id = t.id AND a.user_id = ? AND a.status != ?
                   )
             ORDER BY t.id DESC
            """,
            (user["tg_id"], CANCELLED),
        ) as cur:
            rows = list(await cur.fetchall())

        user_products = {p for p in (user["products"] or "").split(",") if p}
        user_city = user["city_key"]
        has_ip = bool(user["has_ip"])

        suitable = []
        for row in rows:
            check = check_by_code(row["check_code"])
            if check is None or check.product not in user_products:
                continue
            if check.requires_ip is not None and check.requires_ip != has_ip:
                continue
            if not row["remote"] and row["city_key"] != user_city:
                continue
            if await self.taken_slots(row["id"]) >= row["slots"]:
                continue
            # Задание, упёршееся в лимит повторов, не показываем вовсе:
            # предлагать то, что нельзя взять, хуже, чем не предлагать.
            if check.max_per_user or check.cooldown_days:
                taken, days = await self.check_history(user["tg_id"], check.code)
                if check.max_per_user and taken >= check.max_per_user:
                    continue
                if (
                    check.cooldown_days
                    and days is not None
                    and days < check.cooldown_days
                ):
                    continue
            suitable.append(row)
        return suitable

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

    async def check_history(self, user_id: int, check_code: str) -> tuple[int, int | None]:
        """Сколько раз человек уже брал такую проверку и сколько дней назад.

        Возвращает (количество, дней с последнего взятия). Второе — None,
        если человек такую проверку ещё не брал. Нужно для cooldown_days и
        max_per_user: там, где каждое повторение что-то стоит человеку.
        """
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS taken, MAX(a.claimed_at) AS last_at
              FROM assignments a
              JOIN tasks t ON t.id = a.task_id
             WHERE a.user_id = ? AND t.check_code = ? AND a.status != ?
            """,
            (user_id, check_code, CANCELLED),
        ) as cur:
            row = await cur.fetchone()

        if row is None or not row["taken"]:
            return 0, None

        days: int | None = None
        if row["last_at"]:
            try:
                last = datetime.fromisoformat(row["last_at"])
            except ValueError:
                return int(row["taken"]), None
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days = max((datetime.now(timezone.utc) - last).days, 0)
        return int(row["taken"]), days

    async def set_ack(self, assignment_id: int) -> None:
        """Фиксирует, что исполнитель подтвердил предупреждения по заданию."""
        await self.conn.execute(
            "UPDATE assignments SET ack_at = ? WHERE id = ?", (_now(), assignment_id)
        )

    async def get_assignment(self, assignment_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            """
            SELECT a.*, t.title, t.check_code, t.bank, t.city, t.remote,
                   t.payment, t.deadline, t.notes, t.link,
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
            SELECT a.*, t.title, t.city, t.payment, t.deadline, t.check_code, t.bank
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
            SELECT a.*, t.title, t.city, t.payment, t.check_code, t.bank,
                   u.username, u.full_name
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

    # ------------------------------------------------------------ напоминания

    async def assignments_in_work(self) -> list[aiosqlite.Row]:
        """Взятые и недосданные задания, у которых есть машинная дата срока."""
        async with self.conn.execute(
            f"""
            SELECT a.id, a.user_id, a.status, t.id AS task_id, t.title,
                   t.due_date, t.check_code, t.bank, t.payment
              FROM assignments a
              JOIN tasks t ON t.id = a.task_id
             WHERE a.status IN ('{CLAIMED}', '{REWORK}')
               AND t.due_date != ''
            """
        ) as cur:
            return list(await cur.fetchall())

    async def assignments_for_followup(self) -> list[aiosqlite.Row]:
        """Принятые и оплаченные задания — для напоминаний после сдачи.

        Нужны там, где у человека остаётся дело после отчёта: закрыть ИП и
        счёт. Дата отсчитывается от момента приёмки.
        """
        async with self.conn.execute(
            f"""
            SELECT a.id, a.user_id, a.status, a.decided_at, t.title,
                   t.check_code, t.bank
              FROM assignments a
              JOIN tasks t ON t.id = a.task_id
             WHERE a.status IN ('{ACCEPTED}', '{PAID}')
               AND a.decided_at IS NOT NULL
            """
        ) as cur:
            return list(await cur.fetchall())

    async def reminder_sent(self, assignment_id: int, kind: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM reminders WHERE assignment_id = ? AND kind = ?",
            (assignment_id, kind),
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_reminder(self, assignment_id: int, kind: str) -> bool:
        """Отмечает напоминание отправленным.

        Возвращает False, если оно уже было отмечено — тогда отправлять
        второй раз не нужно. Запись делается до отправки, поэтому даже
        двойной запуск бота не пришлёт человеку дубль.
        """
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO reminders (assignment_id, kind, sent_at) "
            "VALUES (?, ?, ?)",
            (assignment_id, kind, _now()),
        )
        return cur.rowcount > 0

    # ----------------------------------------------------------- report items

    async def clear_report(self, assignment_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM report_items WHERE assignment_id = ?", (assignment_id,)
        )

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

    async def list_report_items(self, assignment_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM report_items WHERE assignment_id = ? ORDER BY position, id",
            (assignment_id,),
        ) as cur:
            return list(await cur.fetchall())
