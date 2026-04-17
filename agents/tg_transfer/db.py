import aiosqlite
import uuid
from typing import Optional

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
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_messages (
    job_id      TEXT NOT NULL,
    message_id  INTEGER NOT NULL,
    grouped_id  INTEGER,
    status      TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    error       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
        await self._db.commit()

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
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        await self._db.execute(
            "INSERT INTO jobs (job_id, source_chat, target_chat, mode, filter_type, filter_value) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, source_chat, target_chat, mode, filter_type, filter_value),
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
        await self._db.commit()

    async def set_auto_skip(self, job_id: str, auto_skip: bool):
        await self._db.execute(
            "UPDATE jobs SET auto_skip = ? WHERE job_id = ?", (int(auto_skip), job_id)
        )
        await self._db.commit()

    async def get_running_jobs(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM jobs WHERE status = 'running'") as cur:
            return [dict(row) for row in await cur.fetchall()]

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
