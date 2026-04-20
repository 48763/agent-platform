import aiosqlite
from typing import Optional

_MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    media_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256          TEXT NOT NULL,
    phash           TEXT,
    file_type       TEXT NOT NULL,
    file_size       INTEGER,
    caption         TEXT,
    source_chat     TEXT NOT NULL,
    source_msg_id   INTEGER NOT NULL,
    target_chat     TEXT NOT NULL,
    target_msg_id   INTEGER,
    status          TEXT DEFAULT 'pending',
    job_id          TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_checked_at TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_media_sha256_target
    ON media(sha256, target_chat);
CREATE INDEX IF NOT EXISTS idx_media_phash ON media(phash);
CREATE INDEX IF NOT EXISTS idx_media_caption ON media(caption);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);

CREATE TABLE IF NOT EXISTS tags (
    tag_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS media_tags (
    media_id INTEGER NOT NULL,
    tag_id   INTEGER NOT NULL,
    PRIMARY KEY (media_id, tag_id),
    FOREIGN KEY (media_id) REFERENCES media(media_id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
);
"""


class MediaDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(_MEDIA_SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # -- Media CRUD --

    async def insert_media(
        self, sha256: str, phash: str | None, file_type: str, file_size: int | None,
        caption: str | None, source_chat: str, source_msg_id: int,
        target_chat: str, job_id: str | None = None,
    ) -> int:
        async with self._db.execute(
            "INSERT INTO media (sha256, phash, file_type, file_size, caption, "
            "source_chat, source_msg_id, target_chat, job_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sha256, phash, file_type, file_size, caption, source_chat,
             source_msg_id, target_chat, job_id),
        ) as cur:
            media_id = cur.lastrowid
        await self._db.commit()
        return media_id

    async def get_media(self, media_id: int) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM media WHERE media_id = ?", (media_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def mark_uploaded(self, media_id: int, target_msg_id: int):
        await self._db.execute(
            "UPDATE media SET status = 'uploaded', target_msg_id = ?, "
            "last_checked_at = CURRENT_TIMESTAMP WHERE media_id = ?",
            (target_msg_id, media_id),
        )
        await self._db.commit()

    async def mark_skipped(self, media_id: int):
        await self._db.execute(
            "UPDATE media SET status = 'skipped' WHERE media_id = ?", (media_id,)
        )
        await self._db.commit()

    async def mark_failed(self, media_id: int):
        await self._db.execute(
            "UPDATE media SET status = 'failed' WHERE media_id = ?", (media_id,)
        )
        await self._db.commit()

    async def upsert_pending(
        self, sha256: str, phash: str | None, file_type: str,
        file_size: int | None, caption: str | None, source_chat: str,
        source_msg_id: int, target_chat: str, job_id: str | None = None,
    ) -> int | None:
        """Insert a new pending row, OR revive an existing non-uploaded row
        (failed/skipped/pending) by updating it back to pending.

        Returns the media_id. If an 'uploaded' row already exists for
        (sha256, target_chat), returns None so the caller can branch to the
        dedup path without overwriting the uploaded record.
        """
        async with self._db.execute(
            "INSERT INTO media (sha256, phash, file_type, file_size, caption, "
            "source_chat, source_msg_id, target_chat, job_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending') "
            "ON CONFLICT(sha256, target_chat) DO UPDATE SET "
            "  status='pending', "
            "  phash=excluded.phash, "
            "  file_type=excluded.file_type, "
            "  file_size=excluded.file_size, "
            "  caption=excluded.caption, "
            "  source_chat=excluded.source_chat, "
            "  source_msg_id=excluded.source_msg_id, "
            "  job_id=excluded.job_id "
            "WHERE media.status != 'uploaded' "
            "RETURNING media_id",
            (sha256, phash, file_type, file_size, caption, source_chat,
             source_msg_id, target_chat, job_id),
        ) as cur:
            row = await cur.fetchone()
        await self._db.commit()
        return row["media_id"] if row else None

    async def delete_media(self, media_id: int):
        await self._db.execute("DELETE FROM media WHERE media_id = ?", (media_id,))
        # media_tags rows auto-cascade via FK ON DELETE CASCADE. Tags that
        # no longer have ANY media linking to them become orphans — drop
        # them so total_tags / dashboard reflect reality.
        await self._db.execute(
            "DELETE FROM tags WHERE tag_id NOT IN "
            "(SELECT DISTINCT tag_id FROM media_tags)"
        )
        await self._db.commit()

    # -- Dedup --

    async def find_by_sha256(self, sha256: str, target_chat: str) -> Optional[dict]:
        """Return an uploaded media row for this (sha256, target_chat), if any.
        Only 'uploaded' status blocks re-upload; other statuses mean 待上傳
        and should allow retry."""
        async with self._db.execute(
            "SELECT * FROM media WHERE sha256 = ? AND target_chat = ? "
            "AND status = 'uploaded' ORDER BY media_id DESC LIMIT 1",
            (sha256, target_chat),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_all_phashes(self) -> list[dict]:
        async with self._db.execute(
            "SELECT media_id, phash, caption, target_chat, target_msg_id "
            "FROM media WHERE phash IS NOT NULL AND status = 'uploaded'"
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    # -- Tags --

    async def add_tags(self, media_id: int, tag_names: list[str]):
        for name in tag_names:
            await self._db.execute(
                "INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,)
            )
            async with self._db.execute(
                "SELECT tag_id FROM tags WHERE name = ?", (name,)
            ) as cur:
                row = await cur.fetchone()
                tag_id = row["tag_id"]
            await self._db.execute(
                "INSERT OR IGNORE INTO media_tags (media_id, tag_id) VALUES (?, ?)",
                (media_id, tag_id),
            )
        await self._db.commit()

    async def get_tags(self, media_id: int) -> list[str]:
        async with self._db.execute(
            "SELECT t.name FROM tags t JOIN media_tags mt ON t.tag_id = mt.tag_id "
            "WHERE mt.media_id = ?", (media_id,)
        ) as cur:
            return [row["name"] for row in await cur.fetchall()]

    # -- Search --

    async def search_keyword(self, keyword: str, page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
        offset = (page - 1) * page_size
        query = """
            SELECT DISTINCT m.media_id, m.caption, m.target_chat, m.target_msg_id, m.created_at
            FROM media m
            LEFT JOIN media_tags mt ON m.media_id = mt.media_id
            LEFT JOIN tags t ON mt.tag_id = t.tag_id
            WHERE m.status = 'uploaded' AND (m.caption LIKE ? OR t.name LIKE ?)
            ORDER BY m.created_at DESC
        """
        like = f"%{keyword}%"
        async with self._db.execute(query, (like, like)) as cur:
            all_rows = [dict(row) for row in await cur.fetchall()]
        total = len(all_rows)
        page_rows = all_rows[offset:offset + page_size]
        return page_rows, total

    # -- Stats --

    async def get_stats(self) -> dict:
        by_status = {"uploaded": 0, "pending": 0, "failed": 0, "skipped": 0}
        async with self._db.execute(
            "SELECT status, COUNT(*) as cnt FROM media GROUP BY status"
        ) as cur:
            for row in await cur.fetchall():
                by_status[row["status"]] = row["cnt"]
        total_media = by_status.get("uploaded", 0)

        # Breakdown by media kind (photo / video / document / ...) — only
        # uploaded rows, so failed / skipped attempts don't pad the numbers.
        by_type: dict[str, int] = {}
        async with self._db.execute(
            "SELECT file_type, COUNT(*) as cnt FROM media "
            "WHERE status = 'uploaded' GROUP BY file_type"
        ) as cur:
            for row in await cur.fetchall():
                by_type[row["file_type"]] = row["cnt"]

        # Count only tags that are still linked to an uploaded media — orphans
        # from deleted / unfinished transfers shouldn't inflate the total.
        async with self._db.execute(
            "SELECT COUNT(DISTINCT t.tag_id) as cnt FROM tags t "
            "JOIN media_tags mt ON t.tag_id = mt.tag_id "
            "JOIN media m ON mt.media_id = m.media_id "
            "WHERE m.status = 'uploaded'"
        ) as cur:
            total_tags = (await cur.fetchone())["cnt"]
        async with self._db.execute(
            "SELECT t.name, COUNT(mt.media_id) as cnt FROM tags t "
            "JOIN media_tags mt ON t.tag_id = mt.tag_id "
            "JOIN media m ON mt.media_id = m.media_id AND m.status = 'uploaded' "
            "GROUP BY t.name ORDER BY cnt DESC"
        ) as cur:
            tag_counts = [(row["name"], row["cnt"]) for row in await cur.fetchall()]
        return {
            "total_media": total_media,
            "total_tags": total_tags,
            "tag_counts": tag_counts,
            "by_status": by_status,
            "by_type": by_type,
        }

    # -- Liveness --

    async def get_stale_media(self, max_age_hours: int = 24, limit: int = 50) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM media WHERE status = 'uploaded' "
            "AND (last_checked_at IS NULL OR last_checked_at < datetime('now', ? || ' hours')) "
            "LIMIT ?",
            (f"-{max_age_hours}", limit),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def update_last_checked(self, media_id: int):
        await self._db.execute(
            "UPDATE media SET last_checked_at = CURRENT_TIMESTAMP WHERE media_id = ?",
            (media_id,),
        )
        await self._db.commit()
