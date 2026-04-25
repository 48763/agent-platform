import json

import aiosqlite
from typing import Optional

_MEDIA_TABLES = """
CREATE TABLE IF NOT EXISTS media (
    media_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256          TEXT,
    phash           TEXT,
    thumb_phash     TEXT,
    duration        INTEGER,
    trust           TEXT DEFAULT 'full',
    verified_by     TEXT,
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

-- Phase 4 ambiguous queue: thumb_phash hit target but metadata disagreed,
-- so we parked the source message here for user to resolve at end of batch
-- (Phase 5). candidate_target_msg_ids is a JSON array of possible matches
-- from the target chat.
CREATE TABLE IF NOT EXISTS pending_dedup (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                     TEXT,
    source_chat                TEXT NOT NULL,
    source_msg_id              INTEGER NOT NULL,
    candidate_target_msg_ids   TEXT NOT NULL,
    reason                     TEXT,
    created_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Phase 6 deferred queue: `/batch --skip-dedup` scans the source chat and
-- records thumb_phash + metadata here WITHOUT comparing to target or
-- uploading. Later `/process_deferred` drains this table: for each row, do
-- the Phase-4 thumb lookup against target index, then upload / skip / park
-- in pending_dedup accordingly. Trade-off: scan is cheap (thumbnails only),
-- but you need to run process_deferred before duplicates actually land.
CREATE TABLE IF NOT EXISTS deferred_dedup (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat     TEXT NOT NULL,
    source_msg_id   INTEGER NOT NULL,
    target_chat     TEXT NOT NULL,
    thumb_phash     TEXT,
    file_type       TEXT,
    file_size       INTEGER,
    caption         TEXT,
    duration        INTEGER,
    grouped_id      INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_chat, source_msg_id, target_chat)
);
"""

# Indexes run AFTER _migrate() so a legacy `media` table (missing
# thumb_phash) has the column added before we try to index it.
_MEDIA_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_sha256_target
    ON media(sha256, target_chat);
CREATE INDEX IF NOT EXISTS idx_media_phash ON media(phash);
CREATE INDEX IF NOT EXISTS idx_media_thumb_phash ON media(thumb_phash);
CREATE INDEX IF NOT EXISTS idx_media_caption ON media(caption);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);
CREATE INDEX IF NOT EXISTS idx_pending_dedup_job ON pending_dedup(job_id);
CREATE INDEX IF NOT EXISTS idx_deferred_dedup_target
    ON deferred_dedup(target_chat);
CREATE INDEX IF NOT EXISTS idx_deferred_dedup_thumb
    ON deferred_dedup(thumb_phash);
"""


class MediaDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(_MEDIA_TABLES)
        await self._migrate()
        await self._db.executescript(_MEDIA_INDEXES)
        await self._db.commit()

    async def _migrate(self):
        """Upgrade legacy media tables in-place.

        Legacy DBs have `sha256 TEXT NOT NULL` and no
        thumb_phash/duration/trust/verified_by columns. The scan path
        (Phase 2) creates rows from TG thumbnails alone, so sha256 must
        become nullable. SQLite can't ALTER a NOT NULL constraint in
        place — the only portable fix is table-rebuild: create a new
        table with the current schema, copy rows, swap names.
        """
        async with self._db.execute("PRAGMA table_info(media)") as cur:
            info = list(await cur.fetchall())
        cols = {row["name"]: row for row in info}

        needs_rebuild = (
            "sha256" in cols and cols["sha256"]["notnull"] == 1
        )
        missing = {
            "thumb_phash", "duration", "trust", "verified_by",
        } - set(cols.keys())

        if needs_rebuild:
            # Rebuild: create _new with full schema, copy, swap.
            await self._db.execute("PRAGMA foreign_keys = OFF")
            await self._db.executescript(
                """
                CREATE TABLE media_new (
                    media_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    sha256          TEXT,
                    phash           TEXT,
                    thumb_phash     TEXT,
                    duration        INTEGER,
                    trust           TEXT DEFAULT 'full',
                    verified_by     TEXT,
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
                INSERT INTO media_new (
                    media_id, sha256, phash, file_type, file_size, caption,
                    source_chat, source_msg_id, target_chat, target_msg_id,
                    status, job_id, created_at, last_checked_at
                )
                SELECT
                    media_id, sha256, phash, file_type, file_size, caption,
                    source_chat, source_msg_id, target_chat, target_msg_id,
                    status, job_id, created_at, last_checked_at
                FROM media;
                DROP TABLE media;
                ALTER TABLE media_new RENAME TO media;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_media_sha256_target
                    ON media(sha256, target_chat);
                CREATE INDEX IF NOT EXISTS idx_media_phash ON media(phash);
                CREATE INDEX IF NOT EXISTS idx_media_thumb_phash
                    ON media(thumb_phash);
                CREATE INDEX IF NOT EXISTS idx_media_caption ON media(caption);
                CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);
                """
            )
            await self._db.execute("PRAGMA foreign_keys = ON")
        elif missing:
            # Non-legacy but pre-Phase-1 DB: columns just need to be added.
            # (Unlikely branch, but covers future intermediate versions.)
            type_map = {
                "thumb_phash": "TEXT",
                "duration": "INTEGER",
                "trust": "TEXT DEFAULT 'full'",
                "verified_by": "TEXT",
            }
            for col in missing:
                await self._db.execute(
                    f"ALTER TABLE media ADD COLUMN {col} {type_map[col]}"
                )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_thumb_phash "
                "ON media(thumb_phash)"
            )

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

    async def insert_thumb_record(
        self, thumb_phash: str | None, file_type: str, file_size: int | None,
        caption: str | None, duration: int | None,
        target_chat: str, target_msg_id: int,
    ) -> int:
        """Record a media that already lives in the target chat, indexed via
        its TG thumbnail. sha256/phash are unknown (we never downloaded the
        full file), so trust starts as 'thumb_only'. Re-scanning the same
        (target_chat, target_msg_id) refreshes the metadata in place instead
        of creating a second row, so /index_target is safely resumable and
        re-runnable.

        source_chat/source_msg_id are required NOT NULL by the legacy schema
        but have no natural value for scan rows — we park them as ''/0.
        """
        async with self._db.execute(
            "SELECT media_id FROM media WHERE target_chat = ? "
            "AND target_msg_id = ?",
            (target_chat, target_msg_id),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            await self._db.execute(
                "UPDATE media SET thumb_phash = ?, file_type = ?, "
                "file_size = ?, caption = ?, duration = ? "
                "WHERE media_id = ?",
                (thumb_phash, file_type, file_size, caption, duration,
                 existing["media_id"]),
            )
            await self._db.commit()
            return existing["media_id"]

        async with self._db.execute(
            "INSERT INTO media (thumb_phash, file_type, file_size, caption, "
            "duration, source_chat, source_msg_id, target_chat, "
            "target_msg_id, status, trust) "
            "VALUES (?, ?, ?, ?, ?, '', 0, ?, ?, 'uploaded', 'thumb_only') "
            "RETURNING media_id",
            (thumb_phash, file_type, file_size, caption, duration,
             target_chat, target_msg_id),
        ) as cur:
            row = await cur.fetchone()
        await self._db.commit()
        return row["media_id"]

    async def find_by_thumb_phash(
        self, thumb_phash: str, target_chat: str,
    ) -> list[dict]:
        """Exact thumb_phash matches scoped to a target chat. The caller is
        expected to cross-validate via caption/file_size/duration before
        trusting any of these as a true dedup hit (thumb collisions exist)."""
        async with self._db.execute(
            "SELECT * FROM media WHERE thumb_phash = ? AND target_chat = ? "
            "ORDER BY media_id ASC",
            (thumb_phash, target_chat),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def upgrade_thumb_to_full(
        self, media_id: int, verified_by: str,
    ):
        """Promote a thumb_only row to trust='full' after cross-validating via
        metadata. We do NOT backfill sha256/phash — the caller never downloaded
        the file, so those stay unknown. `verified_by` records which signal
        confirmed the identity (e.g. 'metadata', 'phash', 'sha256').
        """
        await self._db.execute(
            "UPDATE media SET trust = 'full', verified_by = ? "
            "WHERE media_id = ?",
            (verified_by, media_id),
        )
        await self._db.commit()

    async def insert_pending_dedup(
        self, job_id: str | None, source_chat: str, source_msg_id: int,
        candidate_target_msg_ids: list[int], reason: str,
    ) -> int:
        """Queue an ambiguous source message for Phase 5 user resolution.
        thumb_phash hit candidates in the target chat but at least one piece of
        metadata disagreed — we refuse to auto-skip OR auto-upload and let the
        user arbitrate at batch-end."""
        async with self._db.execute(
            "INSERT INTO pending_dedup (job_id, source_chat, source_msg_id, "
            "candidate_target_msg_ids, reason) VALUES (?, ?, ?, ?, ?) "
            "RETURNING id",
            (job_id, source_chat, source_msg_id,
             json.dumps(candidate_target_msg_ids), reason),
        ) as cur:
            row = await cur.fetchone()
        await self._db.commit()
        return row["id"]

    async def list_pending_dedup_by_job(self, job_id: str) -> list[dict]:
        """Return queued ambiguous rows for a job, with candidate_target_msg_ids
        parsed back into a Python list."""
        async with self._db.execute(
            "SELECT * FROM pending_dedup WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ) as cur:
            rows = [dict(row) for row in await cur.fetchall()]
        for r in rows:
            r["candidate_target_msg_ids"] = json.loads(
                r["candidate_target_msg_ids"]
            )
        return rows

    async def delete_pending_dedup(self, row_id: int):
        await self._db.execute(
            "DELETE FROM pending_dedup WHERE id = ?", (row_id,)
        )
        await self._db.commit()

    # -- Phase 6 deferred queue --

    async def insert_deferred_dedup(
        self, source_chat: str, source_msg_id: int, target_chat: str,
        thumb_phash: str | None, file_type: str | None,
        file_size: int | None, caption: str | None,
        duration: int | None, grouped_id: int | None,
    ) -> int:
        """Record one source message's metadata for later dedup comparison.

        Uses INSERT OR REPLACE on (source_chat, source_msg_id, target_chat)
        so re-running a scan over the same source→target pair refreshes the
        row instead of duplicating it.
        """
        async with self._db.execute(
            "INSERT OR REPLACE INTO deferred_dedup "
            "(source_chat, source_msg_id, target_chat, thumb_phash, file_type, "
            "file_size, caption, duration, grouped_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (source_chat, source_msg_id, target_chat, thumb_phash, file_type,
             file_size, caption, duration, grouped_id),
        ) as cur:
            row = await cur.fetchone()
        await self._db.commit()
        return row["id"]

    async def list_deferred_dedup(
        self, source_chat: str | None = None,
        target_chat: str | None = None,
    ) -> list[dict]:
        """Return deferred rows, optionally scoped to a source and/or target
        chat. No scoping → return everything (used by /process_deferred to
        drain the full queue)."""
        clauses = []
        params: list = []
        if source_chat:
            clauses.append("source_chat = ?")
            params.append(source_chat)
        if target_chat:
            clauses.append("target_chat = ?")
            params.append(target_chat)
        sql = "SELECT * FROM deferred_dedup"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        async with self._db.execute(sql, tuple(params)) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def count_deferred_dedup(self) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) AS n FROM deferred_dedup"
        ) as cur:
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def delete_deferred_dedup(self, row_id: int):
        await self._db.execute(
            "DELETE FROM deferred_dedup WHERE id = ?", (row_id,)
        )
        await self._db.commit()

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

    async def get_all_phashes(
        self,
        file_type: str | None = None,
        target_chat: str | None = None,
    ) -> list[dict]:
        """Return uploaded rows that have a phash set.

        Optional `file_type` / `target_chat` scope the result so the dedup
        path can avoid comparing across mismatched media types (e.g. a
        video's phash CSV against a photo's single-frame phash) or
        bleeding one target's index into another's decisions. Both
        filters default to None for backward compat with callers that
        want every uploaded phash row."""
        query = (
            "SELECT media_id, phash, caption, target_chat, target_msg_id, "
            "file_type, file_size "
            "FROM media WHERE phash IS NOT NULL AND status = 'uploaded'"
        )
        params: list = []
        if file_type is not None:
            query += " AND file_type = ?"
            params.append(file_type)
        if target_chat is not None:
            query += " AND target_chat = ?"
            params.append(target_chat)
        async with self._db.execute(query, params) as cur:
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
