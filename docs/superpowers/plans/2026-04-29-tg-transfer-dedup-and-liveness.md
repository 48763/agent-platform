# TG Transfer Dedup Fix + Liveness Loop Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the bug where re-running a completed batch re-downloads everything, plus rewrite the liveness loop into a persistent-plan-file scan that fully cycles every 24h, supports restart-resume, and detects caption/tag edits on the target side.

**Architecture:** Switch `get_transferred_message_ids` from `job_messages` (wiped at terminal status) to `media` table (the durable upload index). Liveness loop becomes: write a JSON plan file under `tmp/.liveness/<uuid>.json` listing every uploaded `media_id`, pop 50 per iteration, atomic-rewrite the file, repeat until empty, sleep literally 24h, then build a fresh plan. Restart automatically resumes from any plan file present. Schema migration adds `last_updated_at` (bumped only when a real edit is detected) and drops the old `last_checked_at` column.

**Tech Stack:** Python 3.12, aiosqlite, Telethon (mocked in tests), pytest-asyncio.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `agents/tg_transfer/media_db.py` | Modify | Schema migration (`last_checked_at` → `last_updated_at`); add `list_all_uploaded_ids` + `update_caption_and_tags`; drop `get_stale_media` + `update_last_checked` |
| `agents/tg_transfer/db.py` | Modify | `get_transferred_message_ids` queries `media` table |
| `agents/tg_transfer/liveness_checker.py` | Rewrite | Plan-file driven scan; pop 50 + atomic rewrite; 24h sleep; detect caption diff + tag re-extract |
| `tests/test_media_db.py` | Modify | Tests for the new helpers + migration |
| `tests/test_db.py` | Modify | Test the dedup query change |
| `tests/test_liveness.py` | Replace | Drop `check_batch`-era tests; add plan-lifecycle + edit-detect + resume tests |

---

## Task 1: Schema migration — `last_checked_at` → `last_updated_at`

**Files:**
- Modify: `agents/tg_transfer/media_db.py:6-26` (`_MEDIA_TABLES`)
- Modify: `agents/tg_transfer/media_db.py:108-192` (`_migrate`)
- Test: `tests/test_media_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_media_db.py`:

```python
@pytest.mark.asyncio
async def test_migration_adds_last_updated_at_with_value_from_created_at(tmp_path):
    """A legacy media row with last_checked_at=NULL must end up with
    last_updated_at = created_at after migration."""
    import aiosqlite
    db_path = str(tmp_path / "legacy.db")

    # Hand-build a legacy schema row
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE media (
                media_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT, phash TEXT, file_type TEXT NOT NULL,
                file_size INTEGER, caption TEXT,
                source_chat TEXT NOT NULL, source_msg_id INTEGER NOT NULL,
                target_chat TEXT NOT NULL, target_msg_id INTEGER,
                status TEXT DEFAULT 'pending', job_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TIMESTAMP
            )
            """
        )
        await conn.execute(
            "INSERT INTO media (sha256, file_type, source_chat, source_msg_id, "
            "target_chat, status, created_at) VALUES "
            "('s1', 'photo', 's', 1, 't', 'uploaded', '2024-01-01 00:00:00')"
        )
        await conn.commit()

    # Run init → triggers migration
    mdb = MediaDB(db_path)
    await mdb.init()
    try:
        async with mdb._db.execute("PRAGMA table_info(media)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "last_updated_at" in cols
        async with mdb._db.execute(
            "SELECT last_updated_at, created_at FROM media WHERE source_msg_id=1"
        ) as cur:
            row = await cur.fetchone()
        assert row["last_updated_at"] == row["created_at"]
    finally:
        await mdb.close()


@pytest.mark.asyncio
async def test_migration_drops_last_checked_at_column_when_supported(tmp_path):
    """On SQLite >= 3.35, last_checked_at must be physically removed."""
    import sqlite3
    sqlite_version = tuple(int(x) for x in sqlite3.sqlite_version.split('.'))

    mdb = MediaDB(str(tmp_path / "x.db"))
    await mdb.init()
    try:
        async with mdb._db.execute("PRAGMA table_info(media)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if sqlite_version >= (3, 35, 0):
            assert "last_checked_at" not in cols
        # Always present in either case:
        assert "last_updated_at" in cols
    finally:
        await mdb.close()


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path):
    """Running init twice must not fail or re-create columns."""
    db_path = str(tmp_path / "y.db")
    mdb1 = MediaDB(db_path)
    await mdb1.init()
    await mdb1.close()
    mdb2 = MediaDB(db_path)
    await mdb2.init()  # must not raise
    try:
        async with mdb2._db.execute("PRAGMA table_info(media)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "last_updated_at" in cols
    finally:
        await mdb2.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_media_db.py::test_migration_adds_last_updated_at_with_value_from_created_at tests/test_media_db.py::test_migration_drops_last_checked_at_column_when_supported tests/test_media_db.py::test_migration_idempotent -v`

Expected: FAIL — `last_updated_at` column doesn't exist yet.

- [ ] **Step 3: Update `_MEDIA_TABLES` schema declaration**

In `agents/tg_transfer/media_db.py`, change line 25 from:

```
    last_checked_at TIMESTAMP
);
```

to:

```
    last_updated_at TIMESTAMP
);
```

(Keep the comma/semicolon structure correct — `last_checked_at` was the last column.)

- [ ] **Step 4: Add migration logic in `_migrate`**

In `agents/tg_transfer/media_db.py::_migrate`, after the existing `needs_rebuild` / `missing` blocks (around line 192), append:

```python
        # last_checked_at → last_updated_at migration. Old code bumped
        # last_checked_at on every scan (whether or not anything changed);
        # the new model only bumps last_updated_at on real edits. Rename
        # by adding the new column with COALESCE'd initial value, then
        # drop the old one (SQLite >= 3.35).
        async with self._db.execute("PRAGMA table_info(media)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}

        if "last_updated_at" not in cols:
            await self._db.execute(
                "ALTER TABLE media ADD COLUMN last_updated_at TIMESTAMP"
            )
            await self._db.execute(
                "UPDATE media SET last_updated_at = "
                "COALESCE(last_checked_at, created_at) "
                "WHERE last_updated_at IS NULL"
            )

        if "last_checked_at" in cols:
            import sqlite3
            sqlite_version = tuple(
                int(x) for x in sqlite3.sqlite_version.split('.')
            )
            if sqlite_version >= (3, 35, 0):
                await self._db.execute(
                    "ALTER TABLE media DROP COLUMN last_checked_at"
                )
            # else: leave dead column; new code never reads/writes it.
```

The `WHERE last_updated_at IS NULL` guard makes the UPDATE idempotent (only fills NULLs). Note the `needs_rebuild` branch on line 122-175 already creates the table without `last_checked_at` if the rebuild path runs after this change is shipped — adjust that block too:

In the rebuild block (around line 134-153), change the `media_new` table definition's last column from `last_checked_at TIMESTAMP` to `last_updated_at TIMESTAMP`, and adjust the INSERT/SELECT pair (lines 154-163) to copy `last_checked_at AS last_updated_at` if the source still has the old name:

```python
INSERT INTO media_new (
    media_id, sha256, phash, file_type, file_size, caption,
    source_chat, source_msg_id, target_chat, target_msg_id,
    status, job_id, created_at, last_updated_at
)
SELECT
    media_id, sha256, phash, file_type, file_size, caption,
    source_chat, source_msg_id, target_chat, target_msg_id,
    status, job_id, created_at,
    COALESCE(last_checked_at, created_at) AS last_updated_at
FROM media;
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_media_db.py -v`

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add agents/tg_transfer/media_db.py tests/test_media_db.py
git commit -m "refactor(tg-transfer): migrate media.last_checked_at to last_updated_at"
```

---

## Task 2: New `media_db` helpers + remove obsolete ones

**Files:**
- Modify: `agents/tg_transfer/media_db.py` (add new helpers; drop old)
- Test: `tests/test_media_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_media_db.py`:

```python
@pytest.mark.asyncio
async def test_list_all_uploaded_ids_returns_only_uploaded(mdb):
    pending = await mdb.insert_media(
        sha256="p1", phash=None, file_type="photo", file_size=10,
        caption=None, source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    uploaded = await mdb.insert_media(
        sha256="u1", phash=None, file_type="photo", file_size=10,
        caption=None, source_chat="s", source_msg_id=2,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(uploaded, target_msg_id=200)
    failed = await mdb.insert_media(
        sha256="f1", phash=None, file_type="photo", file_size=10,
        caption=None, source_chat="s", source_msg_id=3,
        target_chat="t", job_id="j",
    )
    await mdb.mark_failed(failed)

    ids = await mdb.list_all_uploaded_ids()
    assert ids == [uploaded]


@pytest.mark.asyncio
async def test_update_caption_and_tags_replaces_caption_and_tags(mdb):
    media_id = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption="old #foo #bar", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(media_id, target_msg_id=10)
    await mdb.add_tags(media_id, ["foo", "bar"])

    await mdb.update_caption_and_tags(media_id, "new #baz")

    row = await mdb.get_media(media_id)
    assert row["caption"] == "new #baz"
    tags = await mdb.get_tags(media_id)
    assert tags == ["baz"]


@pytest.mark.asyncio
async def test_update_caption_and_tags_bumps_last_updated_at(mdb):
    media_id = await mdb.insert_media(
        sha256="b", phash=None, file_type="photo", file_size=1,
        caption="old", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(media_id, target_msg_id=10)
    before = (await mdb.get_media(media_id))["last_updated_at"]
    # Sleep a tick so timestamp comparison is meaningful at second resolution
    import asyncio
    await asyncio.sleep(1.05)
    await mdb.update_caption_and_tags(media_id, "new content")
    after = (await mdb.get_media(media_id))["last_updated_at"]
    assert after > before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_media_db.py::test_list_all_uploaded_ids_returns_only_uploaded tests/test_media_db.py::test_update_caption_and_tags_replaces_caption_and_tags tests/test_media_db.py::test_update_caption_and_tags_bumps_last_updated_at -v`

Expected: FAIL — methods don't exist.

- [ ] **Step 3: Implement the new helpers**

In `agents/tg_transfer/media_db.py`, add these methods. Place `list_all_uploaded_ids` near other read helpers (around line 460, near `find_by_sha256`); place `update_caption_and_tags` near the tag block (after `add_tags`, around line 519):

```python
    async def list_all_uploaded_ids(self) -> list[int]:
        """Every media_id with status='uploaded'. Used by the liveness loop
        to build a scan plan covering all live target rows. Returned in
        ascending media_id order so plan files are stable across restarts."""
        async with self._db.execute(
            "SELECT media_id FROM media WHERE status = 'uploaded' "
            "ORDER BY media_id ASC"
        ) as cur:
            return [row["media_id"] for row in await cur.fetchall()]
```

```python
    async def update_caption_and_tags(self, media_id: int, caption: str):
        """Persist a detected caption edit:
        1. UPDATE media.caption + bump last_updated_at
        2. DELETE the row's media_tags entries
        3. Re-extract tags from the new caption and INSERT them

        Called by the liveness loop when it sees the target message's
        current caption differs from the stored one."""
        from agents.tg_transfer.tag_extractor import extract_tags

        await self._db.execute(
            "UPDATE media SET caption = ?, "
            "last_updated_at = CURRENT_TIMESTAMP "
            "WHERE media_id = ?",
            (caption, media_id),
        )
        await self._db.execute(
            "DELETE FROM media_tags WHERE media_id = ?", (media_id,),
        )
        await self._db.commit()

        tags = extract_tags(caption)
        if tags:
            await self.add_tags(media_id, tags)
```

- [ ] **Step 4: Remove obsolete helpers and the old `check_batch` test imports**

In `agents/tg_transfer/media_db.py`, delete:
- `get_stale_media` (around line 594-601)
- `update_last_checked` (around line 603-608)

In `tests/test_liveness.py`, the existing tests (`test_check_batch_alive`, `test_check_batch_dead`) reference `check_batch` which is also being removed in Task 4. They will be fully replaced in Task 4's test file rewrite — for now, leave them; they will go red after Task 4's `liveness_checker.py` rewrite, and Task 4 supplies replacements.

For this task, just confirm `media_db.update_last_checked` and `get_stale_media` callers are limited to `liveness_checker.py` (which Task 4 rewrites):

Run: `grep -rn "update_last_checked\|get_stale_media" agents/ tests/ | grep -v __pycache__`

Expected: only references inside `liveness_checker.py` (Task 4 will clean these up). Test files may show old test names — acceptable, Task 4 handles them.

- [ ] **Step 5: Run media_db tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_media_db.py -v`

Expected: all PASS (the three new tests + existing tests).

- [ ] **Step 6: Commit**

```bash
git add agents/tg_transfer/media_db.py tests/test_media_db.py
git commit -m "feat(tg-transfer): add list_all_uploaded_ids + update_caption_and_tags"
```

---

## Task 3: Fix `get_transferred_message_ids` to query `media` table

**Files:**
- Modify: `agents/tg_transfer/db.py:326-333`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

This requires a test that involves both `TransferDB` (jobs/job_messages) AND `MediaDB` (media) since the dedup-source query is moving between the two databases. Append to `tests/test_db.py`:

```python
@pytest.mark.asyncio
async def test_get_transferred_message_ids_reads_from_media_table(tmp_path):
    """Completed jobs have job_messages WIPED, so the dedup query MUST
    read from the persistent media table instead. This is the bug fix:
    re-running the same batch after completion should see prior msg_ids
    and skip them entirely (no thumb download)."""
    from agents.tg_transfer.media_db import MediaDB

    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()
    mdb = MediaDB(str(tmp_path / "m.db"))
    await mdb.init()

    # Simulate a completed job lifecycle: job + job_messages were created,
    # transfer succeeded, media rows were written, then the job went
    # terminal and job_messages was wiped.
    job_id = await db.create_job(
        source_chat="src", target_chat="tgt", mode="batch", task_id="task-1",
    )
    await db.add_messages(job_id, [101, 102, 103])
    for mid in (101, 102, 103):
        await db.mark_message(job_id, mid, "success")
    # Mirror the engine's media-row writes for this job's successes:
    for mid in (101, 102, 103):
        media_id = await mdb.insert_media(
            sha256=f"h{mid}", phash=None, file_type="photo", file_size=10,
            caption=None, source_chat="src", source_msg_id=mid,
            target_chat="tgt", job_id=job_id,
        )
        await mdb.mark_uploaded(media_id, target_msg_id=mid + 1000)
    # Job goes terminal — wipes job_messages
    await db.update_job_status(job_id, "completed")

    # The bug under fix: this used to read from job_messages and return
    # an empty set. After the fix, it reads from media and returns the
    # source_msg_ids of every uploaded row for (src, tgt).
    ids = await db.get_transferred_message_ids("src", "tgt", media_db=mdb)
    assert ids == {101, 102, 103}

    # Filter scope: a different (src, tgt) pair must not pollute results.
    other_ids = await db.get_transferred_message_ids("src", "other", media_db=mdb)
    assert other_ids == set()

    await db.close()
    await mdb.close()


@pytest.mark.asyncio
async def test_get_transferred_message_ids_excludes_non_uploaded(tmp_path):
    """A media row with status != 'uploaded' (failed, pending) must NOT
    count as transferred — re-running the batch should retry those."""
    from agents.tg_transfer.media_db import MediaDB

    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()
    mdb = MediaDB(str(tmp_path / "m.db"))
    await mdb.init()

    # Pending row
    pending_id = await mdb.insert_media(
        sha256="p", phash=None, file_type="photo", file_size=10,
        caption=None, source_chat="src", source_msg_id=200,
        target_chat="tgt", job_id="j",
    )
    # Uploaded row
    uploaded_id = await mdb.insert_media(
        sha256="u", phash=None, file_type="photo", file_size=10,
        caption=None, source_chat="src", source_msg_id=201,
        target_chat="tgt", job_id="j",
    )
    await mdb.mark_uploaded(uploaded_id, target_msg_id=999)
    # Failed row
    failed_id = await mdb.insert_media(
        sha256="f", phash=None, file_type="photo", file_size=10,
        caption=None, source_chat="src", source_msg_id=202,
        target_chat="tgt", job_id="j",
    )
    await mdb.mark_failed(failed_id)

    ids = await db.get_transferred_message_ids("src", "tgt", media_db=mdb)
    assert ids == {201}

    await db.close()
    await mdb.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_db.py::test_get_transferred_message_ids_reads_from_media_table tests/test_db.py::test_get_transferred_message_ids_excludes_non_uploaded -v`

Expected: FAIL — current signature doesn't accept `media_db` param; logic queries job_messages.

- [ ] **Step 3: Modify `get_transferred_message_ids`**

In `agents/tg_transfer/db.py`, replace the existing method (around line 326-333):

```python
    async def get_transferred_message_ids(
        self, source_chat: str, target_chat: str, media_db=None,
    ) -> set[int]:
        """Source message IDs already successfully delivered to the target.

        Reads from the durable `media` table (queried via the supplied
        `media_db` instance), NOT `job_messages` — the latter is wiped
        when a job reaches a terminal status, so completed historical
        jobs would otherwise look like 'never transferred'. Without
        this fix, re-running an identical batch re-downloads everything.

        `media_db` is optional only to keep callers in unit tests that
        don't care about cross-source dedup compiling. Production
        callers (__main__.py:771, :891) pass `self.media_db`.
        """
        if media_db is None:
            return set()
        return await media_db.list_uploaded_source_msg_ids(
            source_chat, target_chat,
        )
```

- [ ] **Step 4: Add the matching `media_db` helper**

In `agents/tg_transfer/media_db.py`, add this near `list_all_uploaded_ids`:

```python
    async def list_uploaded_source_msg_ids(
        self, source_chat: str, target_chat: str,
    ) -> set[int]:
        """Source-side message IDs already uploaded to (source_chat,
        target_chat). Used by the dedup gate at batch-start to skip any
        message we've already delivered, even after the originating
        job's job_messages rows have been wiped."""
        async with self._db.execute(
            "SELECT source_msg_id FROM media "
            "WHERE source_chat = ? AND target_chat = ? "
            "AND status = 'uploaded'",
            (source_chat, target_chat),
        ) as cur:
            return {row["source_msg_id"] for row in await cur.fetchall()}
```

- [ ] **Step 5: Update both callers to pass `media_db`**

In `agents/tg_transfer/__main__.py`, find the two callers via `grep -n "get_transferred_message_ids" agents/tg_transfer/__main__.py`. Both need `media_db=self.media_db` added.

Around line 771:

```python
already_done = await self.db.get_transferred_message_ids(
    source, target, media_db=self.media_db,
)
```

Around line 891:

```python
already_done = await self.db.get_transferred_message_ids(
    job["source_chat"], job["target_chat"], media_db=self.media_db,
)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_db.py tests/test_media_db.py -v`

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add agents/tg_transfer/db.py agents/tg_transfer/media_db.py agents/tg_transfer/__main__.py tests/test_db.py
git commit -m "fix(tg-transfer): dedup query reads media table, surviving job termination"
```

---

## Task 4: Rewrite `liveness_checker.py` with plan-file driven scan

**Files:**
- Replace: `agents/tg_transfer/liveness_checker.py`
- Replace: `tests/test_liveness.py`

- [ ] **Step 1: Write the failing tests (full replacement of `tests/test_liveness.py`)**

Replace the entire content of `tests/test_liveness.py` with:

```python
import asyncio
import json
import os

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agents.tg_transfer.liveness_checker import (
    LIVENESS_DIR,
    create_plan,
    load_plan,
    save_plan,
    locate_or_create_plan,
    process_one,
    run_one_scan,
)
from agents.tg_transfer.media_db import MediaDB


@pytest_asyncio.fixture
async def mdb(tmp_path):
    db = MediaDB(str(tmp_path / "liveness_test.db"))
    await db.init()
    yield db
    await db.close()


def _liveness_root(tmp_path):
    return str(tmp_path / "tmp")


@pytest.mark.asyncio
async def test_create_plan_writes_file_with_all_uploaded_ids(mdb, tmp_path):
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption=None, source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    m2 = await mdb.insert_media(
        sha256="b", phash=None, file_type="photo", file_size=1,
        caption=None, source_chat="s", source_msg_id=2,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=10)
    await mdb.mark_uploaded(m2, target_msg_id=11)

    root = _liveness_root(tmp_path)
    path = await create_plan(mdb, tmp_root=root)

    assert os.path.exists(path)
    assert os.path.dirname(path).endswith(LIVENESS_DIR)
    plan = load_plan(path)
    assert set(plan["remaining"]) == {m1, m2}
    assert "scan_id" in plan
    assert "started_at" in plan


def test_save_plan_uses_atomic_rename(tmp_path):
    """save_plan must write to a .tmp file then rename, so a crash mid-write
    leaves the previous (good) file in place."""
    root = str(tmp_path / "tmp")
    os.makedirs(os.path.join(root, LIVENESS_DIR), exist_ok=True)
    path = os.path.join(root, LIVENESS_DIR, "abc.json")
    save_plan(path, {"scan_id": "abc", "started_at": "x", "remaining": [1, 2, 3]})

    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    assert load_plan(path)["remaining"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_locate_or_create_plan_resumes_existing(mdb, tmp_path):
    """If a plan file already exists, locate_or_create_plan must return it
    untouched instead of creating a new one (this is how restart-resume
    works)."""
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption=None, source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=10)

    root = _liveness_root(tmp_path)
    os.makedirs(os.path.join(root, LIVENESS_DIR), exist_ok=True)
    pre_existing = os.path.join(root, LIVENESS_DIR, "pre.json")
    save_plan(pre_existing, {
        "scan_id": "pre", "started_at": "earlier", "remaining": [999],
    })

    found = await locate_or_create_plan(mdb, tmp_root=root)
    assert found == pre_existing
    plan = load_plan(found)
    assert plan["remaining"] == [999]   # NOT replaced with current uploaded ids


@pytest.mark.asyncio
async def test_process_one_deletes_when_message_missing(mdb, tmp_path):
    """If client.get_messages returns None, process_one must delete the
    media row."""
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption=None, source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=50)

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=None)

    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await process_one(client, mdb, m1)

    assert await mdb.get_media(m1) is None


@pytest.mark.asyncio
async def test_process_one_updates_caption_when_changed(mdb, tmp_path):
    """If the target message's caption has changed, process_one must
    update caption + tags + last_updated_at."""
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption="old #foo", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=50)
    await mdb.add_tags(m1, ["foo"])

    msg = MagicMock()
    msg.id = 50
    msg.text = "new caption #bar"
    msg.message = "new caption #bar"

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=msg)

    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await process_one(client, mdb, m1)

    row = await mdb.get_media(m1)
    assert row["caption"] == "new caption #bar"
    assert await mdb.get_tags(m1) == ["bar"]


@pytest.mark.asyncio
async def test_process_one_no_op_when_caption_unchanged(mdb, tmp_path):
    """If the target message's caption matches the stored one, process_one
    must not bump last_updated_at and must keep the row + tags intact."""
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption="same #x", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=50)
    await mdb.add_tags(m1, ["x"])
    before = (await mdb.get_media(m1))["last_updated_at"]
    await asyncio.sleep(1.05)

    msg = MagicMock()
    msg.id = 50
    msg.text = "same #x"
    msg.message = "same #x"
    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=msg)

    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await process_one(client, mdb, m1)

    after = (await mdb.get_media(m1))["last_updated_at"]
    assert after == before


@pytest.mark.asyncio
async def test_process_one_skips_when_row_already_gone(mdb, tmp_path):
    """If the media row was removed by something else (e.g. concurrent
    on_task_deleted), process_one must safely skip without raising."""
    client = AsyncMock()
    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        # 99999 doesn't exist — must not raise
        await process_one(client, mdb, 99999)
    # client.get_messages must not have been called
    client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_run_one_scan_processes_all_and_deletes_plan(mdb, tmp_path):
    """End-to-end: run_one_scan iterates pop-50 until empty, then deletes
    the plan file."""
    ids = []
    for i in range(3):
        mid = await mdb.insert_media(
            sha256=f"h{i}", phash=None, file_type="photo", file_size=1,
            caption=f"c{i}", source_chat="s", source_msg_id=100 + i,
            target_chat="t", job_id="j",
        )
        await mdb.mark_uploaded(mid, target_msg_id=200 + i)
        ids.append(mid)

    msg = MagicMock()
    # Make all messages "alive" with unchanged caption
    def make_msg(target_msg_id):
        m = MagicMock()
        m.id = target_msg_id
        # Stored captions are "c0"/"c1"/"c2"
        idx = target_msg_id - 200
        m.text = f"c{idx}"
        m.message = f"c{idx}"
        return m

    async def fake_get_messages(entity, ids):
        return make_msg(ids)

    client = AsyncMock()
    client.get_messages = AsyncMock(side_effect=fake_get_messages)

    root = _liveness_root(tmp_path)
    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await run_one_scan(client, mdb, tmp_root=root)

    # All rows still present (alive + unchanged)
    for mid in ids:
        assert await mdb.get_media(mid) is not None
    # Plan file deleted
    liveness_dir = os.path.join(root, LIVENESS_DIR)
    assert not os.path.exists(liveness_dir) or os.listdir(liveness_dir) == []


@pytest.mark.asyncio
async def test_run_one_scan_resumes_from_existing_plan(mdb, tmp_path):
    """If a plan file is already there, run_one_scan must drain IT, not
    rebuild from current uploaded ids. Verifies restart-resume."""
    # Insert one media row but DO NOT include it in the pre-existing plan
    m_real = await mdb.insert_media(
        sha256="real", phash=None, file_type="photo", file_size=1,
        caption="c", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m_real, target_msg_id=500)

    # Stale plan only mentions a non-existent media_id
    root = _liveness_root(tmp_path)
    liveness_dir = os.path.join(root, LIVENESS_DIR)
    os.makedirs(liveness_dir, exist_ok=True)
    plan_path = os.path.join(liveness_dir, "stale.json")
    save_plan(plan_path, {
        "scan_id": "stale", "started_at": "x", "remaining": [99999],
    })

    client = AsyncMock()
    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await run_one_scan(client, mdb, tmp_root=root)

    # The real row must still be uploaded — it was NOT in the plan, so
    # the scan never touched it.
    assert await mdb.get_media(m_real) is not None
    # The plan is consumed (99999 silently skipped via process_one's
    # guard) and deleted.
    assert os.listdir(liveness_dir) == []
    # client.get_messages never called — 99999 doesn't resolve to a row
    client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_run_one_scan_atomic_rewrite_after_each_batch(mdb, tmp_path, monkeypatch):
    """After each pop-of-50, the plan file must be atomically rewritten
    with the trimmed remaining list. Verify by intercepting save_plan."""
    # Force batch size = 2 for this test
    from agents.tg_transfer import liveness_checker
    monkeypatch.setattr(liveness_checker, "BATCH_SIZE", 2)

    ids = []
    for i in range(5):
        mid = await mdb.insert_media(
            sha256=f"h{i}", phash=None, file_type="photo", file_size=1,
            caption=f"c{i}", source_chat="s", source_msg_id=100 + i,
            target_chat="t", job_id="j",
        )
        await mdb.mark_uploaded(mid, target_msg_id=200 + i)
        ids.append(mid)

    saves = []
    real_save_plan = liveness_checker.save_plan

    def spy(path, plan):
        saves.append(list(plan["remaining"]))
        real_save_plan(path, plan)
    monkeypatch.setattr(liveness_checker, "save_plan", spy)

    msg = MagicMock()
    msg.id = 0
    msg.text = ""
    msg.message = ""
    async def fake_get_messages(entity, ids):
        m = MagicMock()
        m.id = ids
        m.text = f"c{ids - 200}"
        m.message = f"c{ids - 200}"
        return m
    client = AsyncMock()
    client.get_messages = AsyncMock(side_effect=fake_get_messages)

    root = _liveness_root(tmp_path)
    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await run_one_scan(client, mdb, tmp_root=root)

    # First save (initial create_plan): 5 ids
    assert saves[0] == ids
    # After first pop-2: 3 remaining
    assert saves[1] == ids[2:]
    # After pop-2 again: 1 remaining
    assert saves[2] == ids[4:]
    # After pop-1 (or pop-2 with only 1 left): 0 remaining
    assert saves[3] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_liveness.py -v`

Expected: FAIL — module doesn't yet have `LIVENESS_DIR`, `create_plan`, `save_plan`, `load_plan`, `locate_or_create_plan`, `process_one`, `run_one_scan`, `BATCH_SIZE`.

- [ ] **Step 3: Rewrite `liveness_checker.py`**

Replace the entire content of `agents/tg_transfer/liveness_checker.py` with:

```python
"""Liveness scanner for the media table.

Runs as a background coroutine started from agent boot. Every cycle:

1. Build (or resume) a plan file at `tmp/.liveness/<uuid>.json` listing
   every media_id with status='uploaded'.
2. Pop BATCH_SIZE ids, check each against the target chat:
   - target msg gone   → delete the media row
   - caption changed   → update_caption_and_tags (re-extract tags)
   - caption unchanged → no-op (last_updated_at NOT bumped)
3. Atomic-rewrite the plan file with the trimmed remaining list.
4. Repeat until plan is empty, then delete the plan file.
5. sleep(SLEEP_INTERVAL_SECONDS) before the next cycle.

Restart safety: a half-consumed plan file is picked up by the next boot
via locate_or_create_plan. The pop-50 → process → rewrite cycle is
idempotent: re-processing a media_id whose row no longer exists is a
safe no-op via process_one's guard.

The plan dir uses a dotfile prefix so existing tmp/ janitors (orphan
scan, legacy migration) skip it automatically.
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from telethon import TelegramClient

from agents.tg_transfer.chat_resolver import resolve_chat
from agents.tg_transfer.media_db import MediaDB

logger = logging.getLogger(__name__)

LIVENESS_DIR = ".liveness"
BATCH_SIZE = 50
SLEEP_INTERVAL_SECONDS = 24 * 3600


def _plan_dir(tmp_root: str) -> str:
    return os.path.join(tmp_root, LIVENESS_DIR)


def load_plan(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_plan(path: str, plan: dict):
    """Atomic-rewrite: write to <path>.tmp then rename. A crash between
    write and rename leaves the previous (good) file in place."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    os.replace(tmp, path)


async def create_plan(media_db: MediaDB, tmp_root: str) -> str:
    """Build a fresh scan plan covering every uploaded media_id."""
    scan_id = str(uuid.uuid4())
    plan = {
        "scan_id": scan_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "remaining": await media_db.list_all_uploaded_ids(),
    }
    path = os.path.join(_plan_dir(tmp_root), f"{scan_id}.json")
    save_plan(path, plan)
    logger.info(
        "Liveness scan %s created with %d media_ids",
        scan_id, len(plan["remaining"]),
    )
    return path


async def locate_or_create_plan(media_db: MediaDB, tmp_root: str) -> str:
    """Return path of the in-progress plan file if one exists; otherwise
    build a new one. This is what makes restart-resume work — a partly
    consumed plan from before reboot picks up where it left off."""
    plan_dir = _plan_dir(tmp_root)
    if os.path.isdir(plan_dir):
        existing = sorted(
            os.path.join(plan_dir, name)
            for name in os.listdir(plan_dir)
            if name.endswith(".json")
        )
        if existing:
            logger.info("Liveness scan resuming from %s", existing[0])
            return existing[0]
    return await create_plan(media_db, tmp_root)


async def process_one(client: TelegramClient, media_db: MediaDB, media_id: int):
    """Check one media_id against the target chat, applying the diff:
    delete row / update caption+tags / no-op."""
    row = await media_db.get_media(media_id)
    if row is None:
        # Row was removed elsewhere (concurrent on_task_deleted etc.).
        return
    target_msg_id = row.get("target_msg_id")
    if target_msg_id is None:
        return
    try:
        target_entity = await resolve_chat(client, row["target_chat"])
        msg = await client.get_messages(target_entity, ids=target_msg_id)
    except Exception as e:
        logger.warning(
            "Liveness check failed for media %d: %s", media_id, e,
        )
        return
    if msg is None:
        await media_db.delete_media(media_id)
        logger.info("Liveness: media %d deleted (target msg gone)", media_id)
        return
    new_caption = (getattr(msg, "text", None)
                   or getattr(msg, "message", None)
                   or "")
    old_caption = row.get("caption") or ""
    if new_caption != old_caption:
        await media_db.update_caption_and_tags(media_id, new_caption)
        logger.info(
            "Liveness: media %d caption updated", media_id,
        )


async def run_one_scan(
    client: TelegramClient, media_db: MediaDB, tmp_root: str,
):
    """Drain a scan plan to completion. Plan file is deleted on success."""
    path = await locate_or_create_plan(media_db, tmp_root)
    while True:
        plan = load_plan(path)
        remaining = plan.get("remaining") or []
        if not remaining:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            logger.info("Liveness scan %s completed", plan.get("scan_id"))
            return
        batch = remaining[:BATCH_SIZE]
        for media_id in batch:
            await process_one(client, media_db, media_id)
        plan["remaining"] = remaining[BATCH_SIZE:]
        save_plan(path, plan)


async def run_liveness_loop(
    client: TelegramClient, media_db: MediaDB, tmp_root: str,
    interval_seconds: int = SLEEP_INTERVAL_SECONDS,
):
    """Background driver: scan to completion, then sleep `interval_seconds`,
    forever. Sleep is fixed regardless of scan duration (per spec)."""
    while True:
        try:
            await run_one_scan(client, media_db, tmp_root)
        except Exception as e:
            logger.error("Liveness loop error: %s", e, exc_info=True)
        await asyncio.sleep(interval_seconds)
```

- [ ] **Step 4: Update the agent boot to pass `tmp_root` and remove the `interval_hours` argument**

In `agents/tg_transfer/__main__.py`, find the `run_liveness_loop` invocation (around line 95):

Before:
```python
interval = settings.get("liveness_check_interval", 24)
asyncio.create_task(run_liveness_loop(self.tg_client, self.media_db, interval))
```

After:
```python
liveness_tmp_root = os.path.join(data_dir, "tmp")
liveness_interval_seconds = int(
    settings.get("liveness_check_interval", 24)
) * 3600
asyncio.create_task(run_liveness_loop(
    self.tg_client, self.media_db, liveness_tmp_root,
    interval_seconds=liveness_interval_seconds,
))
```

`data_dir` is already in scope at this location (used a few lines earlier for `tmp_dir=os.path.join(data_dir, "tmp")` on the engine).

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_liveness.py -v`

Expected: all PASS.

Then run the full project test suite to confirm no regressions:

Run: `PYTHONPATH=. pytest tests/ -v 2>&1 | tail -30`

Expected: 2 pre-existing failures (`test_integration.py`) remain; all liveness/db/media_db tests pass.

- [ ] **Step 6: Commit**

```bash
git add agents/tg_transfer/liveness_checker.py agents/tg_transfer/__main__.py tests/test_liveness.py
git commit -m "feat(tg-transfer): rewrite liveness loop with plan-file driven full scan"
```

---

## Task 5: Manual end-to-end verification + deployment

This task has no test code — it confirms the change works on real infrastructure before declaring done. Per project memory rule "修 bug 後務必 commit + build + deploy 一次完成", this is the deployment gate.

- [ ] **Step 1: Build and restart the affected services**

Run: `docker compose up -d --build tg-transfer-agent`

Expected: container rebuilds, starts, no startup errors in `docker compose logs --tail=80 tg-transfer-agent`. Specifically, look for:
- "Liveness scan <uuid> created with N media_ids" — fresh plan written
- No tracebacks from the migration

- [ ] **Step 2: Verify migration ran**

Run:
```bash
docker compose exec tg-transfer-agent python -c "
import sqlite3
conn = sqlite3.connect('/data/tg_transfer/transfer.db')
cur = conn.cursor()
cur.execute('PRAGMA table_info(media)')
cols = {r[1] for r in cur.fetchall()}
print('last_updated_at present:', 'last_updated_at' in cols)
print('last_checked_at present:', 'last_checked_at' in cols)
"
```

Expected: `last_updated_at present: True`. `last_checked_at present: False` (assuming SQLite >= 3.35; `True` is acceptable on older SQLite — confirmed by checking the version with `python -c "import sqlite3; print(sqlite3.sqlite_version)"`).

- [ ] **Step 3: Verify liveness plan file exists**

Run: `docker compose exec tg-transfer-agent ls -la /data/tg_transfer/tmp/.liveness/`

Expected: a single `<uuid>.json` file (the in-progress scan), or empty directory if the scan already finished. If empty, force a fresh scan to test:

```bash
docker compose exec tg-transfer-agent python -c "
import json, os
from glob import glob
plans = glob('/data/tg_transfer/tmp/.liveness/*.json')
for p in plans:
    print(p, json.load(open(p))['remaining'][:5], '...')"
```

Expected output: file path + first 5 media_ids in `remaining`.

- [ ] **Step 4: Test re-batch dedup fix**

Via Telegram: send the agent a `/batch` command for a small source you've fully transferred before (or run a fresh small batch first to populate, then re-run the same command).

Expected agent reply on the second run: "符合條件的訊息：約 N 則 / 預計搬移：0 則 / 確認執行？（是/否）" — `預計搬移` should be 0, indicating the dedup gate caught all messages from the `media` table.

If you confirm "是", the batch should complete instantly without downloading any thumbs.

Watch logs in another shell:
```bash
docker compose logs -f tg-transfer-agent
```

Expected: no `Pre-dedup` / `Downloading` log lines for the re-batch.

- [ ] **Step 5: Test caption-edit detection (optional, requires manual TG action)**

In the target chat, edit one of the previously-transferred messages' caption (e.g. add `#newtag`). Wait for the next liveness cycle (or restart the agent to force one immediately).

Verify the DB picked up the change:
```bash
docker compose exec tg-transfer-agent python -c "
import sqlite3
conn = sqlite3.connect('/data/tg_transfer/transfer.db')
cur = conn.cursor()
cur.execute(\"SELECT media_id, caption, last_updated_at FROM media WHERE caption LIKE '%newtag%'\")
print(cur.fetchall())
"
```

Expected: the edited row's `caption` contains `#newtag` and `last_updated_at` is recent.

- [ ] **Step 6: Test restart-resume (optional)**

```bash
docker compose restart tg-transfer-agent
docker compose logs --tail=30 tg-transfer-agent | grep -i liveness
```

Expected log line: `Liveness scan resuming from /data/tg_transfer/tmp/.liveness/<uuid>.json` (or `Liveness scan ... completed` if the previous scan happened to finish before restart).

- [ ] **Step 7: Document outcome**

If all steps pass, the deployment is verified. If any step fails, capture the failing log, roll back via `git revert <range>`, and re-deploy.

---

## Self-Review

Spec coverage check:

| Spec section | Implementing task |
|---|---|
| A. dedup query change | Task 3 |
| B.1 directory structure (`.liveness/<uuid>.json`) | Task 4 (constants in liveness_checker.py) |
| B.2 plan file format | Task 4 |
| B.3 scan main loop | Task 4 |
| B.4 one-scan-at-a-time guarantee | Task 4 (via locate_or_create_plan) |
| B.5 restart resume | Task 4 (test_run_one_scan_resumes_from_existing_plan) |
| B.6 process_one logic | Task 4 (process_one + tests) |
| C. schema migration | Task 1 |
| D. dedup hit doesn't bump last_updated_at | Implicit — no caller in transfer_engine writes last_updated_at; only update_caption_and_tags does, and that's only called from liveness |
| Test plan | Tasks 1-4 each have unit tests; Task 5 covers manual e2e |

Type/name consistency check:
- `LIVENESS_DIR`, `BATCH_SIZE`, `SLEEP_INTERVAL_SECONDS` constants: defined in liveness_checker.py, used in tests via the same names ✓
- `list_all_uploaded_ids` (Task 2) consumed by `create_plan` (Task 4) ✓
- `update_caption_and_tags` (Task 2) consumed by `process_one` (Task 4) ✓
- `list_uploaded_source_msg_ids` (Task 3) consumed by `get_transferred_message_ids` (Task 3 same file) ✓
- `media_db` parameter added to `get_transferred_message_ids` (Task 3) — both call sites in `__main__.py` updated in same task ✓

No placeholders, no missing code blocks, no "TBD".

Plan ready.
