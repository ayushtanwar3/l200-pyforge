"""SQLite persistent storage with WAL mode and an async background write worker."""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.config import DEFAULT_CONFIG
from src.observability.pii_scrubber import PIIScrubber
from src.observability.structured_logging import default_logger


@dataclass(frozen=True)
class WriteTask:
    """Represents a queued write operation for the background worker."""

    sql: str
    params: tuple[Any, ...]
    future: asyncio.Future[int] | None = None


class SQLiteDatabase:
    """SQLite persistence layer with WAL concurrency and an asyncio.Queue write worker."""

    def __init__(self, db_path: Path | str = DEFAULT_CONFIG.db_path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[WriteTask | None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """Create a SQLite connection configured with WAL journal mode and Row factory."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        """Initialize WAL mode and create required tables if they do not exist."""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    title TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generated_scripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );
                """
            )
            conn.commit()

    async def start_worker(self) -> None:
        """Start the background asyncio.Queue database write worker."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._write_worker())
        default_logger.info(
            layer="storage",
            intent="start_sqlite_write_worker",
            outcome="started",
            metadata={"db_path": str(self.db_path)},
        )

    async def stop_worker(self) -> None:
        """Drain pending writes and stop the background database write worker."""
        if self._queue is not None and self._worker_task is not None:
            await self._queue.put(None)
            await self._worker_task
            self._worker_task = None
            self._queue = None
            default_logger.info(
                layer="storage",
                intent="stop_sqlite_write_worker",
                outcome="stopped",
                metadata={"db_path": str(self.db_path)},
            )

    async def _write_worker(self) -> None:
        """Process queued SQL write operations sequentially in the background."""
        assert self._queue is not None
        while True:
            task = await self._queue.get()
            if task is None:
                self._queue.task_done()
                break

            try:
                last_row_id = await asyncio.to_thread(
                    self._execute_write_sync, task.sql, task.params
                )
                if task.future and not task.future.done():
                    task.future.set_result(last_row_id)
            except Exception as exc:
                default_logger.error(
                    layer="storage",
                    intent="execute_queued_write",
                    outcome="error",
                    metadata={"error": str(exc)},
                )
                if task.future and not task.future.done():
                    task.future.set_exception(exc)
            finally:
                self._queue.task_done()

    def _execute_write_sync(self, sql: str, params: tuple[Any, ...]) -> int:
        """Execute a single SQL write statement synchronously and return lastrowid."""
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return int(cursor.lastrowid or 0)

    async def enqueue_write(
        self, sql: str, params: tuple[Any, ...], *, wait: bool = True
    ) -> int:
        """Enqueue a write operation onto the async worker queue (or run if worker inactive)."""
        if self._queue is not None and self._worker_task is not None and not self._worker_task.done():
            loop = asyncio.get_running_loop()
            future: asyncio.Future[int] | None = loop.create_future() if wait else None
            await self._queue.put(WriteTask(sql=sql, params=params, future=future))
            if future is not None:
                return await future
            return 0
        return await asyncio.to_thread(self._execute_write_sync, sql, params)

    def create_session(
        self, title: str, session_id: str | None = None
    ) -> str:
        """Create a new session synchronously and return `session_id`."""
        sid = session_id or str(uuid.uuid4())
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        safe_title = PIIScrubber.scrub(title)
        self._execute_write_sync(
            "INSERT OR REPLACE INTO sessions (session_id, created_at, title) VALUES (?, ?, ?)",
            (sid, now, safe_title),
        )
        return sid

    async def create_session_async(
        self, title: str, session_id: str | None = None
    ) -> str:
        """Create a new session via the async write worker queue."""
        sid = session_id or str(uuid.uuid4())
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        safe_title = PIIScrubber.scrub(title)
        await self.enqueue_write(
            "INSERT OR REPLACE INTO sessions (session_id, created_at, title) VALUES (?, ?, ?)",
            (sid, now, safe_title),
        )
        return sid

    def add_message(
        self, session_id: str, role: str, content: str
    ) -> int:
        """Persist a chat message synchronously."""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        safe_content = PIIScrubber.scrub(content)
        return self._execute_write_sync(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, role, safe_content, now),
        )

    async def add_message_async(
        self, session_id: str, role: str, content: str
    ) -> int:
        """Persist a chat message via the async write worker queue."""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        safe_content = PIIScrubber.scrub(content)
        return await self.enqueue_write(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, role, safe_content, now),
        )

    def save_generated_script(
        self,
        *,
        session_id: str,
        name: str,
        spec_json: str,
        code: str,
        status: str,
    ) -> int:
        """Persist a generated script record synchronously."""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        return self._execute_write_sync(
            """
            INSERT INTO generated_scripts (session_id, name, spec_json, code, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                name,
                PIIScrubber.scrub(spec_json),
                code,
                status,
                now,
            ),
        )

    async def save_generated_script_async(
        self,
        *,
        session_id: str,
        name: str,
        spec_json: str,
        code: str,
        status: str,
    ) -> int:
        """Persist a generated script record via the async write worker queue."""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        return await self.enqueue_write(
            """
            INSERT INTO generated_scripts (session_id, name, spec_json, code, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                name,
                PIIScrubber.scrub(spec_json),
                code,
                status,
                now,
            ),
        )

    def get_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Retrieve all messages for a given session ordered by ID."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, session_id, role, content, timestamp FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def list_generated_scripts(self) -> list[dict[str, Any]]:
        """Retrieve all generated scripts ordered from newest to oldest."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT id, session_id, name, spec_json, code, status, created_at
                FROM generated_scripts
                ORDER BY id DESC
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_generated_script_by_name(self, name: str) -> dict[str, Any] | None:
        """Retrieve the most recent generated script matching `name`."""
        normalized = name[:-3] if name.endswith(".py") else name
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT id, session_id, name, spec_json, code, status, created_at
                FROM generated_scripts
                WHERE name = ? OR name = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (normalized, f"{normalized}.py"),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

