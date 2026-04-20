import aiosqlite
import uuid
from typing import Optional

# Statuses after which a job can't be resumed. Reaching any of these triggers
# job_messages cleanup so the per-message rows (potentially thousands) don't
# linger forever; the jobs row itself stays as history.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    source_chat TEXT NOT NULL,
    target_chat TEXT NOT NULL,
    filter_type TEXT,
    filter_value TEXT,
    mode        TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',
    auto_skip   BOOLEAN DEFAULT 0,
    task_id     TEXT,
    chat_id     INTEGER,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_messages (
    job_id           TEXT NOT NULL,
    message_id       INTEGER NOT NULL,
    grouped_id       INTEGER,
    status           TEXT DEFAULT 'pending',
    retry_count      INTEGER DEFAULT 0,
    error            TEXT,
    partial_path     TEXT,
    downloaded_bytes INTEGER DEFAULT 0,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (job_id, message_id),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_messages_status
    ON job_messages(job_id, status);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class TransferDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self):
        """Add columns introduced after initial schema to legacy DBs."""
        async with self._db.execute("PRAGMA table_info(jobs)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "task_id" not in cols:
            await self._db.execute("ALTER TABLE jobs ADD COLUMN task_id TEXT")
        if "chat_id" not in cols:
            await self._db.execute("ALTER TABLE jobs ADD COLUMN chat_id INTEGER")

        async with self._db.execute("PRAGMA table_info(job_messages)") as cur:
            jm_cols = {row["name"] for row in await cur.fetchall()}
        if "partial_path" not in jm_cols:
            await self._db.execute("ALTER TABLE job_messages ADD COLUMN partial_path TEXT")
        if "downloaded_bytes" not in jm_cols:
            await self._db.execute(
                "ALTER TABLE job_messages ADD COLUMN downloaded_bytes INTEGER DEFAULT 0"
            )

    async def close(self):
        if self._db:
            await self._db.close()

    # -- Jobs --

    async def create_job(
        self,
        source_chat: str,
        target_chat: str,
        mode: str,
        filter_type: str = None,
        filter_value: str = None,
        task_id: str = None,
        chat_id: int = None,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        await self._db.execute(
            "INSERT INTO jobs (job_id, source_chat, target_chat, mode, filter_type, filter_value, task_id, chat_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, source_chat, target_chat, mode, filter_type, filter_value, task_id, chat_id),
        )
        await self._db.commit()
        return job_id

    async def get_job(self, job_id: str) -> Optional[dict]:
        async with self._db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_job_status(self, job_id: str, status: str):
        await self._db.execute(
            "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
            (status, job_id),
        )
        if status in _TERMINAL_STATUSES:
            await self._db.execute(
                "DELETE FROM job_messages WHERE job_id = ?", (job_id,),
            )
        await self._db.commit()

    async def set_auto_skip(self, job_id: str, auto_skip: bool):
        await self._db.execute(
            "UPDATE jobs SET auto_skip = ? WHERE job_id = ?", (int(auto_skip), job_id)
        )
        await self._db.commit()

    async def get_running_jobs(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM jobs WHERE status = 'running'") as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def get_resumable_jobs(self) -> list[dict]:
        """Jobs that should be re-attached on agent startup: running (mid-transfer)
        and paused (awaiting user decision). Pending/completed are excluded."""
        async with self._db.execute(
            "SELECT * FROM jobs WHERE status IN ('running', 'paused')"
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def update_job_binding(self, job_id: str, task_id: str, chat_id: int):
        """Rewrite a job's TG task/chat binding. Used when a paused job resumes
        under a new user reply whose task_id differs from the original."""
        await self._db.execute(
            "UPDATE jobs SET task_id = ?, chat_id = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE job_id = ?",
            (task_id, chat_id, job_id),
        )
        await self._db.commit()

    # -- Messages --

    async def add_messages(
        self, job_id: str, message_ids: list[int], grouped_ids: dict[int, int] = None
    ):
        grouped_ids = grouped_ids or {}
        for msg_id in message_ids:
            gid = grouped_ids.get(msg_id)
            await self._db.execute(
                "INSERT OR IGNORE INTO job_messages (job_id, message_id, grouped_id) VALUES (?, ?, ?)",
                (job_id, msg_id, gid),
            )
        await self._db.commit()

    async def get_next_pending(self, job_id: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM job_messages WHERE job_id = ? AND status = 'pending' ORDER BY message_id ASC LIMIT 1",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_message(self, job_id: str, message_id: int) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM job_messages WHERE job_id = ? AND message_id = ?",
            (job_id, message_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def mark_message(self, job_id: str, message_id: int, status: str, error: str = None):
        await self._db.execute(
            "UPDATE job_messages SET status = ?, error = ? WHERE job_id = ? AND message_id = ?",
            (status, error, job_id, message_id),
        )
        await self._db.commit()

    async def increment_retry(self, job_id: str, message_id: int):
        await self._db.execute(
            "UPDATE job_messages SET retry_count = retry_count + 1 WHERE job_id = ? AND message_id = ?",
            (job_id, message_id),
        )
        await self._db.commit()

    async def set_partial(self, job_id: str, message_id: int, path: str, downloaded_bytes: int):
        """Record partial-download state so the next run can resume from
        `downloaded_bytes` via iter_download(offset=...). Called on every
        flush-interval (e.g. 64MB) during a download."""
        await self._db.execute(
            "UPDATE job_messages SET partial_path = ?, downloaded_bytes = ? "
            "WHERE job_id = ? AND message_id = ?",
            (path, downloaded_bytes, job_id, message_id),
        )
        await self._db.commit()

    async def clear_partial(self, job_id: str, message_id: int):
        """Clear partial-download state after successful upload; prevents the
        next run from resuming a file that's already been delivered."""
        await self._db.execute(
            "UPDATE job_messages SET partial_path = NULL, downloaded_bytes = 0 "
            "WHERE job_id = ? AND message_id = ?",
            (job_id, message_id),
        )
        await self._db.commit()

    async def reset_message(self, job_id: str, message_id: int):
        await self._db.execute(
            "UPDATE job_messages SET status = 'pending', error = NULL WHERE job_id = ? AND message_id = ?",
            (job_id, message_id),
        )
        await self._db.commit()

    async def get_grouped_messages(self, job_id: str, grouped_id: int) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM job_messages WHERE job_id = ? AND grouped_id = ? ORDER BY message_id ASC",
            (job_id, grouped_id),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def get_progress(self, job_id: str) -> dict:
        counts = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "pending": 0}
        async with self._db.execute(
            "SELECT status, COUNT(*) as cnt FROM job_messages WHERE job_id = ? GROUP BY status",
            (job_id,),
        ) as cur:
            async for row in cur:
                counts[row["status"]] = row["cnt"]
                counts["total"] += row["cnt"]
        return counts

    # -- Dedup --

    async def get_transferred_message_ids(self, source_chat: str, target_chat: str) -> set[int]:
        async with self._db.execute(
            "SELECT jm.message_id FROM job_messages jm "
            "JOIN jobs j ON jm.job_id = j.job_id "
            "WHERE j.source_chat = ? AND j.target_chat = ? AND jm.status = 'success'",
            (source_chat, target_chat),
        ) as cur:
            return {row["message_id"] for row in await cur.fetchall()}

    # -- Config --

    async def get_config(self, key: str) -> Optional[str]:
        async with self._db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def set_config(self, key: str, value: str):
        await self._db.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._db.commit()
