# TG Transfer Agent 13-Item Optimizations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply 13 audit-identified optimizations to the tg_transfer agent: DB write/read efficiency, event-loop unblocking, hot-path caching, helper hygiene, and a `BatchController` extraction with error boundary that closes the silent-coroutine-death gap.

**Architecture:** Six independent task groups (A-F) ordered by risk: DB infra → engine async → caching → DB helpers → `__main__` misc → BatchController refactor. Each group ships standalone; each task has TDD steps; the final task is a deployment verification gate.

**Tech Stack:** Python 3.12, aiosqlite, asyncio, Telethon (mocked in tests), pytest-asyncio.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `agents/tg_transfer/db.py` | Modify | A1 executemany, A2 WAL pragma, D2 batch_mark_failed_as_skipped |
| `agents/tg_transfer/media_db.py` | Modify | A2 WAL pragma, A3+A4 indexes, D1 search_keyword pagination |
| `agents/tg_transfer/transfer_engine.py` | Modify | B1 to_thread hashes, B2 ffprobe single-call, C1 phash candidate cache param, C2 size-limit TTL |
| `agents/tg_transfer/__main__.py` | Modify | C1 plumb cache, E1 message cache, E2 liveness ref, E3 album window |
| `agents/tg_transfer/batch_controller.py` | Create | F1 BatchController + error boundary |
| `agents/tg_transfer/agent.yaml` | Modify | E3 album_window setting |
| `tests/test_db.py`, `tests/test_media_db.py`, `tests/test_transfer_engine.py`, `tests/test_tg_transfer_integration.py` | Modify | Coverage for each task |
| `tests/test_batch_controller.py` | Create | F1 |

---

## Task 1: Group A — DB schema + write efficiency

**Files:**
- Modify: `agents/tg_transfer/db.py` (`init`, `add_messages`)
- Modify: `agents/tg_transfer/media_db.py` (`init`, `_MEDIA_INDEXES`)
- Test: `tests/test_db.py`, `tests/test_media_db.py`

- [ ] **Step 1: Write the failing tests in `tests/test_db.py`**

Append:

```python
@pytest.mark.asyncio
async def test_add_messages_inserts_all_rows_in_one_batch(db):
    """add_messages should accept large batches and persist all rows
    via executemany (single round-trip rather than N awaits)."""
    job_id = await db.create_job("@s", "@d", "batch")
    ids = list(range(1000, 2000))
    grouped = {1500: 99999, 1501: 99999}
    await db.add_messages(job_id, ids, grouped_ids=grouped)

    msg_first = await db.get_message(job_id, 1000)
    assert msg_first is not None
    msg_grouped = await db.get_message(job_id, 1500)
    assert msg_grouped["grouped_id"] == 99999
    msg_last = await db.get_message(job_id, 1999)
    assert msg_last is not None


@pytest.mark.asyncio
async def test_init_enables_wal_mode(tmp_path):
    """init() must set journal_mode=WAL to avoid serialized writes
    when MediaDB and TransferDB share a file."""
    db = TransferDB(str(tmp_path / "wal.db"))
    await db.init()
    try:
        async with db._db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row[0].lower() == "wal"
    finally:
        await db.close()
```

- [ ] **Step 2: Write the failing tests in `tests/test_media_db.py`**

Append:

```python
@pytest.mark.asyncio
async def test_init_enables_wal_mode_media(tmp_path):
    mdb = MediaDB(str(tmp_path / "wal.db"))
    await mdb.init()
    try:
        async with mdb._db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row[0].lower() == "wal"
    finally:
        await mdb.close()


@pytest.mark.asyncio
async def test_phash_lookup_index_exists(mdb):
    async with mdb._db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_media_phash_lookup'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_target_msg_index_exists(mdb):
    async with mdb._db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_media_target_msg'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_db.py::test_add_messages_inserts_all_rows_in_one_batch tests/test_db.py::test_init_enables_wal_mode tests/test_media_db.py::test_init_enables_wal_mode_media tests/test_media_db.py::test_phash_lookup_index_exists tests/test_media_db.py::test_target_msg_index_exists -v`

Expected: WAL tests FAIL (default journal_mode is `delete`); index tests FAIL (indexes not yet created); add_messages test passes (slow loop) but converting to executemany should keep it green.

- [ ] **Step 4: Implement A1 — `add_messages` via executemany**

Edit `agents/tg_transfer/db.py`. Replace the existing `add_messages`:

```python
    async def add_messages(
        self, job_id: str, message_ids: list[int], grouped_ids: dict[int, int] = None
    ):
        grouped_ids = grouped_ids or {}
        rows = [
            (job_id, msg_id, grouped_ids.get(msg_id))
            for msg_id in message_ids
        ]
        await self._db.executemany(
            "INSERT OR IGNORE INTO job_messages (job_id, message_id, grouped_id) "
            "VALUES (?, ?, ?)",
            rows,
        )
        await self._db.commit()
```

- [ ] **Step 5: Implement A2 — WAL pragma in TransferDB.init**

Edit `agents/tg_transfer/db.py::init`. Locate the existing init body (it sets row_factory + executescript + _migrate + commit). Insert WAL pragma after row_factory and before executescript:

```python
    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()
```

- [ ] **Step 6: Implement A2 — WAL pragma in MediaDB.init + A3+A4 indexes**

Edit `agents/tg_transfer/media_db.py::init`:

```python
    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(_MEDIA_TABLES)
        await self._migrate()
        await self._db.executescript(_MEDIA_INDEXES)
        await self._db.commit()
```

In `_MEDIA_INDEXES` (top of file), append the two new indexes inside the triple-quoted string:

```sql
CREATE INDEX IF NOT EXISTS idx_media_phash_lookup
    ON media(target_chat, file_type, status) WHERE phash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_media_target_msg
    ON media(target_chat, target_msg_id);
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_db.py tests/test_media_db.py -v 2>&1 | tail -10`

Expected: all PASS.

- [ ] **Step 8: Run full suite to confirm no regressions**

Run: `PYTHONPATH=. pytest tests/ 2>&1 | tail -5`

Expected: only the 2 pre-existing `test_integration.py` failures.

- [ ] **Step 9: Commit**

```bash
git add agents/tg_transfer/db.py agents/tg_transfer/media_db.py tests/test_db.py tests/test_media_db.py
git commit -m "perf(tg-transfer): WAL mode + executemany + dedup-lookup indexes"
```

---

## Task 2: Group B — Engine async unblocking + ffprobe consolidation

**Files:**
- Modify: `agents/tg_transfer/transfer_engine.py` (hash call sites + video ffprobe)
- Test: `tests/test_transfer_engine.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transfer_engine.py`:

```python
@pytest.mark.asyncio
async def test_transfer_media_video_runs_ffprobe_only_once(tmp_path, monkeypatch):
    """Video transfer must call ffprobe_metadata exactly once: result is
    reused for both the dedup gate and upload attributes."""
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()

    class FakeClient:
        async def download_media(self, msg, file):
            with open(file, "wb") as f:
                f.write(b"x" * 16)
            return file
        async def send_file(self, *a, **k):
            return type("M", (), {"id": 1})()

    engine = TransferEngine(
        client=FakeClient(), db=db, tmp_dir=str(tmp_path / "tmp"),
    )

    class FakeMsg:
        id = 555
        text = ""
        media = object()
        file = type("F", (), {"size": 16, "name": None, "ext": ".mp4"})()

    monkeypatch.setattr(engine, "should_skip", lambda _m: False)
    monkeypatch.setattr(engine, "_detect_file_type", lambda _m: "video")

    ffprobe_calls = []
    async def fake_ffprobe(path):
        ffprobe_calls.append(path)
        return {"duration": 12, "width": 100, "height": 50}

    monkeypatch.setattr(
        "agents.tg_transfer.transfer_engine.ffprobe_metadata",
        fake_ffprobe,
    )
    async def fake_phash_video(path, tmp_dir):
        return "abc"
    monkeypatch.setattr(
        "agents.tg_transfer.transfer_engine.compute_phash_video",
        fake_phash_video,
    )

    await engine._transfer_media(
        target_entity=None, message=FakeMsg(),
        target_chat="t", source_chat="s",
        job_id=None, skip_pre_dedup=True,
        task_id="task-FF",
    )

    assert len(ffprobe_calls) == 1, f"ffprobe called {len(ffprobe_calls)}× (expected 1)"
    await db.close()


@pytest.mark.asyncio
async def test_transfer_media_hash_calls_run_in_threadpool(tmp_path, monkeypatch):
    """compute_sha256 / compute_phash must be wrapped in asyncio.to_thread
    on the transfer path so the event loop doesn't stall on multi-second
    hash computation for large files."""
    import asyncio as _asyncio
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()

    to_thread_calls = []
    real_to_thread = _asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        to_thread_calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(_asyncio, "to_thread", spy_to_thread)

    class FakeClient:
        async def download_media(self, msg, file):
            with open(file, "wb") as f:
                f.write(b"x" * 16)
            return file
        async def send_file(self, *a, **k):
            return type("M", (), {"id": 1})()

    engine = TransferEngine(
        client=FakeClient(), db=db, tmp_dir=str(tmp_path / "tmp"),
    )

    class FakeMsg:
        id = 1
        text = ""
        media = object()
        file = type("F", (), {"size": 16, "name": None, "ext": ".jpg"})()

    monkeypatch.setattr(engine, "should_skip", lambda _m: False)
    monkeypatch.setattr(engine, "_detect_file_type", lambda _m: "photo")

    await engine._transfer_media(
        target_entity=None, message=FakeMsg(),
        target_chat="t", source_chat="s",
        job_id=None, skip_pre_dedup=True,
        task_id="task-HASH",
    )

    assert "compute_sha256" in to_thread_calls
    assert "compute_phash" in to_thread_calls
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_transfer_engine.py::test_transfer_media_video_runs_ffprobe_only_once tests/test_transfer_engine.py::test_transfer_media_hash_calls_run_in_threadpool -v`

Expected: FAIL.

- [ ] **Step 3: Implement B2 — single ffprobe in `_transfer_media`**

Locate `_transfer_media` in `transfer_engine.py`. Find the FIRST video ffprobe block (around line 805-815, inside the dedup classification gate):

```python
            sha256 = compute_sha256(path)
            file_type = self._detect_file_type(message)
            phash = None
            duration = None
            if file_type == "video":
                phash = await compute_phash_video(path, task_dir)
                meta = await ffprobe_metadata(path)
                if meta:
                    duration = meta.get("duration")
            elif file_type == "photo":
                phash = compute_phash(path)
```

Leave this block as-is (it stores `meta` in scope).

Find the SECOND video ffprobe (around line 868, in the upload attributes block):

```python
            if file_type == "video":
                meta = await ffprobe_metadata(path)
                ...
```

Replace with conditional re-fetch only on miss:

```python
            if file_type == "video":
                # Reuse meta from the dedup block above (single ffprobe
                # per video). Fallback only if the dedup-time call failed.
                if 'meta' not in locals() or meta is None:
                    meta = await ffprobe_metadata(path)
                ...
```

Verify by `grep -n "ffprobe_metadata" agents/tg_transfer/transfer_engine.py` — only the first call should fire under normal flow.

- [ ] **Step 4: Implement B2 — `transfer_album` ffprobe per-file dedup**

Locate `transfer_album` (around line 399-636). The album path computes ffprobe twice: once in the per-file dedup loop (around line 487-498) and once in the upload-prep loop (around line 569-572).

Solution: store `meta` per file in a list aligned with the existing `per_file_attrs` / `per_file_thumbs`.

Find the per-file phash loop (around line 478-500) and add `per_file_meta` collection:

```python
            if self.media_db:
                phash_cands_by_type: dict[str, list[dict]] = {}
                per_file_meta: list[dict | None] = []
                for msg, path in zip(messages, file_paths):
                    sha256 = await asyncio.to_thread(compute_sha256, path)
                    file_type = self._detect_file_type(msg)
                    phash = None
                    duration = None
                    meta = None
                    if file_type == "photo":
                        phash = await asyncio.to_thread(compute_phash, path)
                    elif file_type == "video":
                        phash = await compute_phash_video(path, task_dir)
                        meta = await ffprobe_metadata(path)
                        if meta:
                            duration = meta.get("duration")
                    per_file_meta.append(meta)
                    file_size = (
                        os.path.getsize(path) if os.path.exists(path) else None
                    )
```

(The `await asyncio.to_thread(...)` substitutions are part of B1 below — apply both at once.)

Find the upload-prep loop (around line 555-582) where `meta` is recomputed:

```python
                file_type = self._detect_file_type(msg)
                attrs = None
                thumb = None
                if file_type == "video":
                    meta = await ffprobe_metadata(path)
                    if not meta:
                        meta = _meta_from_message(msg)
                    if meta:
                        attrs = [DocumentAttributeVideo(...)]
```

Replace with use-cached:

```python
                file_type = self._detect_file_type(msg)
                attrs = None
                thumb = None
                if file_type == "video":
                    meta = per_file_meta[idx] if idx < len(per_file_meta) else None
                    if meta is None:
                        meta = await ffprobe_metadata(path)
                    if not meta:
                        meta = _meta_from_message(msg)
                    if meta:
                        attrs = [DocumentAttributeVideo(...)]
```

Note: confirm `idx` is the loop variable name from the existing upload-prep loop. If it's a different name, use that.

- [ ] **Step 5: Implement B1 — wrap hash calls with `asyncio.to_thread`**

Add `import asyncio` at the top of `transfer_engine.py` if not already present.

In `_transfer_media`, locate `sha256 = compute_sha256(path)` and replace:

```python
            sha256 = await asyncio.to_thread(compute_sha256, path)
```

Locate `phash = compute_phash(path)` (the photo branch only — the video branch already uses `await compute_phash_video`) and replace:

```python
            elif file_type == "photo":
                phash = await asyncio.to_thread(compute_phash, path)
```

In `transfer_album`, the per-file dedup loop touched in Step 4 should already include the to_thread-wrapped versions per the code shown there.

Verify with: `grep -n "compute_sha256\|compute_phash[^_]" agents/tg_transfer/transfer_engine.py` — every call should be wrapped in `asyncio.to_thread` (compute_phash_video stays raw — it's already async).

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_transfer_engine.py -v 2>&1 | tail -15`

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add agents/tg_transfer/transfer_engine.py tests/test_transfer_engine.py
git commit -m "perf(tg-transfer): unblock event loop on hash calls + single ffprobe per video"
```

---

## Task 3: Group C — Hot-path caches (phash candidates + size limit TTL)

**Files:**
- Modify: `agents/tg_transfer/transfer_engine.py` (`_size_limit_bytes`, `_transfer_media`, `transfer_single`, `transfer_album`, `run_batch`)
- Test: `tests/test_transfer_engine.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transfer_engine.py`:

```python
@pytest.mark.asyncio
async def test_size_limit_bytes_uses_ttl_cache(tmp_path, monkeypatch):
    """5 quick calls should hit the DB once."""
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()
    await db.set_config("size_limit_mb", "10")

    engine = TransferEngine(
        client=None, db=db, tmp_dir=str(tmp_path / "tmp"),
    )

    select_count = 0
    real_get_config = db.get_config
    async def spy_get_config(key):
        nonlocal select_count
        if key == "size_limit_mb":
            select_count += 1
        return await real_get_config(key)
    monkeypatch.setattr(db, "get_config", spy_get_config)

    for _ in range(5):
        await engine._size_limit_bytes()

    assert select_count == 1, f"expected 1 DB hit, got {select_count}"
    await db.close()


@pytest.mark.asyncio
async def test_transfer_media_skips_phash_fetch_when_candidates_provided(
    tmp_path, monkeypatch,
):
    """When phash_candidates kwarg is supplied, _transfer_media must NOT
    call media_db.get_all_phashes for that (file_type, target_chat)."""
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()

    fetch_calls = []

    class FakeMediaDB:
        async def get_all_phashes(self, file_type=None, target_chat=None):
            fetch_calls.append((file_type, target_chat))
            return []
        async def find_by_sha256(self, *a, **k):
            return None
        async def find_by_thumb_phash(self, *a, **k):
            return []
        async def insert_media(self, *a, **k):
            return 1
        async def mark_uploaded(self, *a, **k):
            pass
        async def add_tags(self, *a, **k):
            pass

    class FakeClient:
        async def download_media(self, msg, file):
            with open(file, "wb") as f:
                f.write(b"x" * 16)
            return file
        async def send_file(self, *a, **k):
            return type("M", (), {"id": 1})()

    engine = TransferEngine(
        client=FakeClient(), db=db, media_db=FakeMediaDB(),
        tmp_dir=str(tmp_path / "tmp"),
    )

    class FakeMsg:
        id = 1
        text = ""
        media = object()
        file = type("F", (), {"size": 16, "name": None, "ext": ".jpg"})()

    monkeypatch.setattr(engine, "should_skip", lambda _m: False)
    monkeypatch.setattr(engine, "_detect_file_type", lambda _m: "photo")

    await engine._transfer_media(
        target_entity=None, message=FakeMsg(),
        target_chat="t", source_chat="s",
        job_id=None, skip_pre_dedup=True,
        task_id="task-X",
        phash_candidates=[],
    )

    assert fetch_calls == [], f"expected no get_all_phashes call, got {fetch_calls}"
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_transfer_engine.py::test_size_limit_bytes_uses_ttl_cache tests/test_transfer_engine.py::test_transfer_media_skips_phash_fetch_when_candidates_provided -v`

Expected: FAIL.

- [ ] **Step 3: Implement C2 — `_size_limit_bytes` TTL cache**

Edit `agents/tg_transfer/transfer_engine.py`. Add `import time` near the other imports.

In `__init__` (around line 156-170), add cache state:

```python
        self._cancelled: set[str] = set()
        # 5-second TTL cache for size_limit_mb. Live edits propagate
        # within ~5s; saves ~999 SELECTs on a 1000-msg batch.
        self._size_limit_cache: tuple[float, int] | None = None
        self._SIZE_LIMIT_TTL = 5.0
```

Replace `_size_limit_bytes`:

```python
    async def _size_limit_bytes(self) -> int:
        """Current per-message byte cap. 0 = no limit. Cached for 5s."""
        now = time.monotonic()
        if self._size_limit_cache is not None:
            cached_at, value = self._size_limit_cache
            if now - cached_at < self._SIZE_LIMIT_TTL:
                return value
        raw = await self.db.get_config("size_limit_mb")
        if not raw:
            value = 0
        else:
            try:
                mb = int(raw)
            except (TypeError, ValueError):
                value = 0
            else:
                value = max(mb, 0) * 1024 * 1024
        self._size_limit_cache = (now, value)
        return value
```

- [ ] **Step 4: Implement C1 — add `phash_candidates` kwarg to `_transfer_media` and `transfer_single`**

Edit `_transfer_media` signature:

```python
    async def _transfer_media(self, target_entity, message, target_chat: str = "",
                               source_chat: str = "", job_id: str = None,
                               skip_pre_dedup: bool = False,
                               task_id: str = None,
                               phash_candidates: list[dict] | None = None) -> dict:
```

Inside `_transfer_media`, find the existing `get_all_phashes` call (around line 831):

```python
                if phash:
                    candidates = await self.media_db.get_all_phashes(
                        file_type=file_type, target_chat=target_chat,
                    )
```

Replace with:

```python
                if phash:
                    if phash_candidates is not None:
                        candidates = phash_candidates
                    else:
                        candidates = await self.media_db.get_all_phashes(
                            file_type=file_type, target_chat=target_chat,
                        )
```

Edit `transfer_single` to forward the kwarg:

```python
    async def transfer_single(self, source_entity, target_entity, message,
                               target_chat: str = "", source_chat: str = "",
                               job_id: str = None,
                               skip_pre_dedup: bool = False,
                               task_id: str = None,
                               phash_candidates: list[dict] | None = None) -> dict:
        ...
        if message.media and not self.should_skip(message):
            return await self._transfer_media(
                target_entity, message, target_chat=target_chat,
                source_chat=source_chat, job_id=job_id,
                skip_pre_dedup=skip_pre_dedup,
                task_id=task_id,
                phash_candidates=phash_candidates,
            )
```

- [ ] **Step 5: Implement C1 — `run_batch` per-batch phash cache**

Edit `agents/tg_transfer/transfer_engine.py::run_batch`. Add cache initialization at the start of the function body (after `processed = 0`):

```python
        # Per-batch phash candidate cache: avoid re-fetching the entire
        # phash table for every message. Lives only for this batch.
        phash_cache: dict[str, list[dict]] = {}

        async def _get_candidates(file_type: str) -> list[dict]:
            if file_type not in phash_cache:
                if self.media_db is None:
                    phash_cache[file_type] = []
                else:
                    phash_cache[file_type] = await self.media_db.get_all_phashes(
                        file_type=file_type, target_chat=job["target_chat"],
                    )
            return phash_cache[file_type]
```

Find the single-message `transfer_single` call (around line 1152) and prefetch candidates:

```python
                        job = await self.db.get_job(job_id)
                        # Determine file_type so we can prefetch its phash
                        # candidates from the per-batch cache.
                        file_type = self._detect_file_type(msg)
                        candidates = (
                            await _get_candidates(file_type)
                            if file_type in ("photo", "video") else None
                        )
                        result = await self.transfer_single(
                            source_entity, target_entity, msg,
                            target_chat=job["target_chat"],
                            source_chat=job["source_chat"],
                            job_id=job_id,
                            task_id=job.get("task_id"),
                            phash_candidates=candidates,
                        )
```

(The album path inside `run_batch` already uses `transfer_album`'s internal `phash_cands_by_type` dict, which is per-album. We leave it untouched — the cross-album benefit would require plumbing a `phash_candidates_by_type` kwarg into `transfer_album`. Skipped for now since albums in a single batch are typically few.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_transfer_engine.py -v 2>&1 | tail -15`

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add agents/tg_transfer/transfer_engine.py tests/test_transfer_engine.py
git commit -m "perf(tg-transfer): batch-scoped phash candidate cache + size_limit_mb TTL"
```

---

## Task 4: Group D — DB helpers cleanup

**Files:**
- Modify: `agents/tg_transfer/db.py` (add `batch_mark_failed_as_skipped`)
- Modify: `agents/tg_transfer/media_db.py::search_keyword`
- Modify: `agents/tg_transfer/__main__.py::_skip_current_failed`
- Test: `tests/test_db.py`, `tests/test_media_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
@pytest.mark.asyncio
async def test_batch_mark_failed_as_skipped_updates_only_failed(db):
    """One UPDATE flips every status='failed' row to 'skipped'."""
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [1, 2, 3, 4])
    await db.mark_message(job_id, 1, "failed", error="x")
    await db.mark_message(job_id, 2, "success")
    await db.mark_message(job_id, 3, "failed", error="y")

    count = await db.batch_mark_failed_as_skipped(job_id)
    assert count == 2

    assert (await db.get_message(job_id, 1))["status"] == "skipped"
    assert (await db.get_message(job_id, 2))["status"] == "success"
    assert (await db.get_message(job_id, 3))["status"] == "skipped"
    assert (await db.get_message(job_id, 4))["status"] == "pending"
```

Append to `tests/test_media_db.py`:

```python
@pytest.mark.asyncio
async def test_search_keyword_uses_sql_pagination(mdb):
    """Inserting > page_size matches: page1 returns page_size, page2
    returns the remainder, no overlap."""
    for i in range(15):
        m = await mdb.insert_media(
            sha256=f"h{i}", phash=None, file_type="photo", file_size=1,
            caption=f"hello world {i}", source_chat="s", source_msg_id=i,
            target_chat="t", job_id="j",
        )
        await mdb.mark_uploaded(m, target_msg_id=1000 + i)

    page1, total1 = await mdb.search_keyword("hello", page=1, page_size=10)
    page2, total2 = await mdb.search_keyword("hello", page=2, page_size=10)

    assert total1 == 15
    assert total2 == 15
    assert len(page1) == 10
    assert len(page2) == 5
    assert {r["media_id"] for r in page1}.isdisjoint(
        {r["media_id"] for r in page2}
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_db.py::test_batch_mark_failed_as_skipped_updates_only_failed tests/test_media_db.py::test_search_keyword_uses_sql_pagination -v`

Expected: `batch_mark_failed_as_skipped` doesn't exist (AttributeError); `search_keyword` test passes already (Python slicing is correct, just inefficient — Step 5 makes it efficient).

- [ ] **Step 3: Implement D2 — `batch_mark_failed_as_skipped`**

Add to `agents/tg_transfer/db.py` (place near `mark_message`):

```python
    async def batch_mark_failed_as_skipped(self, job_id: str) -> int:
        """Flip every job_message status='failed' for this job to 'skipped'
        in one UPDATE. Returns rows touched."""
        cur = await self._db.execute(
            "UPDATE job_messages SET status = 'skipped' "
            "WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
        await self._db.commit()
        return cur.rowcount or 0
```

- [ ] **Step 4: Refactor `_skip_current_failed` to use the helper**

Edit `agents/tg_transfer/__main__.py`. Find `_skip_current_failed` via `grep -n "_skip_current_failed" agents/tg_transfer/__main__.py`. Replace its body:

```python
    async def _skip_current_failed(self, job_id: str):
        """Mark every failed job_message as skipped, single UPDATE.
        No more direct self.db._db.execute access."""
        await self.db.batch_mark_failed_as_skipped(job_id)
```

- [ ] **Step 5: Implement D1 — `search_keyword` two-step pagination**

Edit `agents/tg_transfer/media_db.py::search_keyword`. Replace with:

```python
    async def search_keyword(self, keyword: str, page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
        """Two-step paginated search: COUNT for total + LIMIT/OFFSET for page."""
        offset = (page - 1) * page_size
        like = f"%{keyword}%"

        count_query = """
            SELECT COUNT(DISTINCT m.media_id) AS cnt
            FROM media m
            LEFT JOIN media_tags mt ON m.media_id = mt.media_id
            LEFT JOIN tags t ON mt.tag_id = t.tag_id
            WHERE m.status = 'uploaded' AND (m.caption LIKE ? OR t.name LIKE ?)
        """
        async with self._db.execute(count_query, (like, like)) as cur:
            row = await cur.fetchone()
            total = row["cnt"] if row else 0

        page_query = """
            SELECT DISTINCT m.media_id, m.caption, m.target_chat, m.target_msg_id, m.created_at
            FROM media m
            LEFT JOIN media_tags mt ON m.media_id = mt.media_id
            LEFT JOIN tags t ON mt.tag_id = t.tag_id
            WHERE m.status = 'uploaded' AND (m.caption LIKE ? OR t.name LIKE ?)
            ORDER BY m.created_at DESC
            LIMIT ? OFFSET ?
        """
        async with self._db.execute(
            page_query, (like, like, page_size, offset),
        ) as cur:
            page_rows = [dict(row) for row in await cur.fetchall()]
        return page_rows, total
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_db.py tests/test_media_db.py -v 2>&1 | tail -10`

Expected: all PASS.

- [ ] **Step 7: Run full suite**

Run: `PYTHONPATH=. pytest tests/ 2>&1 | tail -5`

Expected: 2 pre-existing failures only.

- [ ] **Step 8: Commit**

```bash
git add agents/tg_transfer/db.py agents/tg_transfer/media_db.py agents/tg_transfer/__main__.py tests/test_db.py tests/test_media_db.py
git commit -m "refactor(tg-transfer): batch-mark-skipped helper + SQL-paginated search"
```

---

## Task 5: Group E — `__main__.py` minor improvements

**Files:**
- Modify: `agents/tg_transfer/__main__.py`
- Modify: `agents/tg_transfer/agent.yaml`
- Test: `tests/test_tg_transfer_integration.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tg_transfer_integration.py`:

```python
@pytest.mark.asyncio
async def test_batch_message_cache_attribute_exists(tmp_path):
    """_handle_batch_request and _start_batch coordinate via
    self._batch_message_cache: dict[job_id, list[Message]]."""
    agent = await _build_test_agent(tmp_path)
    assert hasattr(agent, "_batch_message_cache")
    assert isinstance(agent._batch_message_cache, dict)


@pytest.mark.asyncio
async def test_batch_message_cache_consumed_and_evicted(tmp_path):
    """When a job_id is in the cache, callers consume it via
    pop(); after consume the entry is gone."""
    agent = await _build_test_agent(tmp_path)
    fake_msgs = [type("M", (), {"id": i, "grouped_id": None})() for i in range(3)]
    agent._batch_message_cache["job-X"] = fake_msgs

    cached = agent._batch_message_cache.pop("job-X", None)
    assert cached is fake_msgs
    assert "job-X" not in agent._batch_message_cache
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_tg_transfer_integration.py::test_batch_message_cache_attribute_exists tests/test_tg_transfer_integration.py::test_batch_message_cache_consumed_and_evicted -v`

Expected: FAIL — `_batch_message_cache` doesn't exist on the agent.

- [ ] **Step 3: Implement E1 — batch message cache attribute**

Edit `agents/tg_transfer/__main__.py`. In `__init__`, after the existing in-memory dicts (around line 47-51), add:

```python
        # job_id → list[Message] cached between _handle_batch_request
        # (which iterates the source for the count + dedup preview) and
        # _start_batch (which would otherwise re-iterate). Evicted on
        # consume or cancel.
        self._batch_message_cache: dict[str, list] = {}
```

In `_handle_batch_request`, find the message collection (look for `_count_messages` or `_collect_messages` calls). The current flow likely uses `_count_messages` for the preview. Switch to one collection:

Find this block (or similar):

```python
        count = await self._count_messages(source_entity, filter_type, filter_value)
```

Replace with:

```python
        # Collect once and reuse for both preview and _start_batch.
        collected_messages = await self._collect_messages(
            source_entity, filter_type, filter_value,
        )
        count = len(collected_messages)
```

After `job_id = await self.db.create_job(...)` in the same function, store in cache:

```python
        self._pending_jobs[task.task_id] = job_id
        self._batch_message_cache[job_id] = collected_messages
```

In `_start_batch`, find the existing `messages = await self._collect_messages(...)` and replace:

```python
        # Prefer the cache from _handle_batch_request; fall back to
        # re-iterate if cache miss (e.g., agent restarted between).
        messages = self._batch_message_cache.pop(job_id, None)
        if messages is None:
            filter_type = job["filter_type"] or "all"
            filter_value = (
                json.loads(job["filter_value"]) if job["filter_value"] else None
            )
            messages = await self._collect_messages(
                source_entity, filter_type, filter_value,
            )
```

Add `self._batch_message_cache.pop(job_id, None)` to cancel/failure paths in `_handle_paused_response` (where it dels `_pending_jobs`).

- [ ] **Step 4: Implement E2 — liveness task ref + respawn callback**

Edit `agents/tg_transfer/__main__.py`. In `__init__`, locate the `asyncio.create_task(run_liveness_loop(...))` call and replace:

```python
        liveness_tmp_root = os.path.join(data_dir, "tmp")
        liveness_interval_seconds = int(
            settings.get("liveness_check_interval", 24)
        ) * 3600
        # Save params for respawn callback
        self._data_dir = data_dir
        self._liveness_interval_seconds = liveness_interval_seconds
        self._liveness_task = asyncio.create_task(run_liveness_loop(
            self.tg_client, self.media_db, liveness_tmp_root,
            interval_seconds=liveness_interval_seconds,
        ))
        self._liveness_task.add_done_callback(self._on_liveness_done)
```

Add the callback method on the class:

```python
    def _on_liveness_done(self, task: asyncio.Task):
        """The only legitimate exit is CancelledError on shutdown.
        Anything else is unexpected — log and respawn."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            logger.warning(
                "Liveness loop exited without exception — unexpected; respawning"
            )
        else:
            logger.error(
                "Liveness loop crashed: %s; respawning", exc, exc_info=exc,
            )
        liveness_tmp_root = os.path.join(self._data_dir, "tmp")
        self._liveness_task = asyncio.create_task(run_liveness_loop(
            self.tg_client, self.media_db, liveness_tmp_root,
            interval_seconds=self._liveness_interval_seconds,
        ))
        self._liveness_task.add_done_callback(self._on_liveness_done)
```

- [ ] **Step 5: Implement E3 — `album_window` setting + grouped_id-aware detection**

Edit `agents/tg_transfer/agent.yaml`. Find the existing `settings:` block. Add:

```yaml
  album_window: 10  # neighbouring message-id range when detecting album siblings (Telethon iter_messages window)
```

Edit `agents/tg_transfer/__main__.py`. In `__init__`, read the setting:

```python
        self._album_window = int(settings.get("album_window", 10))
```

Find the album detection logic via `grep -n "grouped_id\|range.*-.*+\|_resolve_album" agents/tg_transfer/__main__.py | head -10`. Locate the specific spot in `_handle_single` where album siblings are gathered (around line 499 area in the prior version).

Add a helper on the class:

```python
    async def _resolve_album_messages(self, source_entity, msg) -> list:
        """Return all messages in `msg`'s album. Filter by grouped_id
        within a configurable id window (default 10) — exact match avoids
        false positives from neighbouring non-album messages."""
        grouped_id = getattr(msg, "grouped_id", None)
        if not grouped_id:
            return [msg]
        window = self._album_window
        try:
            siblings = []
            async for sib in self.tg_client.iter_messages(
                source_entity,
                min_id=max(msg.id - window - 1, 0),
                max_id=msg.id + window + 1,
            ):
                if getattr(sib, "grouped_id", None) == grouped_id:
                    siblings.append(sib)
            siblings.sort(key=lambda m: m.id)
            return siblings or [msg]
        except Exception as e:
            logger.warning(
                "Album detection via grouped_id failed (%s); falling back to ±%d window",
                e, window,
            )
            try:
                ids = list(range(msg.id - window, msg.id + window + 1))
                msgs = await self.tg_client.get_messages(source_entity, ids=ids)
                return [
                    m for m in (msgs or [])
                    if m and getattr(m, "grouped_id", None) == grouped_id
                ] or [msg]
            except Exception:
                return [msg]
```

Find the existing inline album detection in `_handle_single` (likely an `iter_messages` or `get_messages` call with a hardcoded range) and replace with `await self._resolve_album_messages(source_entity, msg)`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_tg_transfer_integration.py -v 2>&1 | tail -10`

Expected: all PASS.

- [ ] **Step 7: Run full suite**

Run: `PYTHONPATH=. pytest tests/ 2>&1 | tail -5`

Expected: 2 pre-existing failures only.

- [ ] **Step 8: Commit**

```bash
git add agents/tg_transfer/__main__.py agents/tg_transfer/agent.yaml tests/test_tg_transfer_integration.py
git commit -m "refactor(tg-transfer): batch message cache + liveness respawn + album_window setting"
```

---

## Task 6: Group F — `BatchController` extract + error boundary

**Files:**
- Create: `agents/tg_transfer/batch_controller.py`
- Modify: `agents/tg_transfer/__main__.py`
- Test: `tests/test_batch_controller.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_batch_controller.py`:

```python
import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from agents.tg_transfer.batch_controller import BatchController
from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.media_db import MediaDB


@pytest_asyncio.fixture
async def stub_agent(tmp_path):
    """Minimal agent stub providing the references BatchController reads."""
    agent = MagicMock()
    agent.db = TransferDB(str(tmp_path / "t.db"))
    await agent.db.init()
    agent.media_db = MediaDB(str(tmp_path / "m.db"))
    await agent.media_db.init()
    agent.engine = MagicMock()
    agent.tg_client = MagicMock()
    agent._pending_jobs = {}
    agent._current_chat_id = {}
    agent._batch_message_cache = {}
    agent.ws_send_progress = AsyncMock()
    agent.ws_send_result = AsyncMock()
    yield agent
    await agent.db.close()
    await agent.media_db.close()


@pytest.mark.asyncio
async def test_batch_controller_tracks_bg_task(stub_agent):
    """spawn_batch should store the asyncio.Task in _bg_tasks under task_id."""
    controller = BatchController(stub_agent)

    async def fake_run(task_id, job_id, *args, **kwargs):
        await asyncio.sleep(0.01)

    controller._run_batch_background = fake_run

    bg = controller.spawn_batch(
        task_id="t-1", job_id="j-1", job={}, source_entity=None,
        target_entity=None, chat_id=0,
    )
    assert "t-1" in controller._bg_tasks
    await bg
    await asyncio.sleep(0)
    # The wrapper's finally removes it
    assert "t-1" not in controller._bg_tasks


@pytest.mark.asyncio
async def test_batch_controller_error_boundary_reports_to_ws(stub_agent):
    """Uncaught exception in a background coroutine must:
    1. log
    2. mark job 'failed' in DB
    3. send AgentResult(ERROR, ...) via ws_send_result"""
    controller = BatchController(stub_agent)

    job_id = await stub_agent.db.create_job(
        source_chat="s", target_chat="t", mode="batch",
        task_id="t-err", chat_id=42,
    )
    await stub_agent.db.update_job_status(job_id, "running")
    stub_agent._current_chat_id["t-err"] = 42

    async def boom(task_id, job_id_param, *args, **kwargs):
        raise RuntimeError("simulated batch crash")

    controller._run_batch_background = boom

    bg = controller.spawn_batch(
        task_id="t-err", job_id=job_id, job={}, source_entity=None,
        target_entity=None, chat_id=42,
    )
    with pytest.raises(RuntimeError):
        await bg

    job_row = await stub_agent.db.get_job(job_id)
    assert job_row["status"] == "failed"
    assert stub_agent.ws_send_result.called
    args, kwargs = stub_agent.ws_send_result.call_args
    sent_result = args[1] if len(args) > 1 else kwargs.get("result")
    assert "ERROR" in str(sent_result.status)
    assert "simulated batch crash" in sent_result.message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest tests/test_batch_controller.py -v`

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `agents/tg_transfer/batch_controller.py`**

Create the file:

```python
"""Batch lifecycle controller for the tg_transfer agent.

Owns the three background coroutines:
- _run_batch_background — main batch transfer driver
- _run_defer_scan_background — `/batch --skip-dedup` scan
- _run_process_deferred_background — `/process_deferred` drain

Plus the spawn_* helpers wrapping each in an error-boundary that:
1. logs the exception
2. marks the job 'failed' in TransferDB
3. sends AgentResult(ERROR, ...) to the user via agent.ws_send_result

Without this boundary, an uncaught exception silently kills the
asyncio.Task and leaves the job stuck 'running' with no user
notification.

The controller does NOT own _pending_jobs / _current_chat_id /
_batch_message_cache — those stay on the agent because cancel /
dedup-response handlers also touch them. The controller reads them
via the agent reference.
"""
import asyncio
import logging

from core.models import AgentResult, TaskStatus

logger = logging.getLogger(__name__)


class BatchController:
    def __init__(self, agent):
        self.agent = agent
        self._bg_tasks: dict[str, asyncio.Task] = {}

    # -- spawn API ----------------------------------------------------------

    def spawn_batch(self, task_id, job_id, job, source_entity, target_entity, chat_id):
        wrapped = self._wrap_with_error_boundary(
            self._run_batch_background(
                task_id, job_id, job, source_entity, target_entity, chat_id,
            ),
            task_id=task_id, job_id=job_id, chat_id=chat_id,
        )
        bg = asyncio.create_task(wrapped)
        self._bg_tasks[task_id] = bg
        return bg

    def spawn_defer_scan(self, task_id, job_id, job, messages, chat_id):
        wrapped = self._wrap_with_error_boundary(
            self._run_defer_scan_background(
                task_id, job_id, job, messages, chat_id,
            ),
            task_id=task_id, job_id=job_id, chat_id=chat_id,
        )
        bg = asyncio.create_task(wrapped)
        self._bg_tasks[task_id] = bg
        return bg

    def spawn_process_deferred(
        self, task_id, job_id, job, rows,
        source_entity, target_entity, chat_id,
    ):
        wrapped = self._wrap_with_error_boundary(
            self._run_process_deferred_background(
                task_id, job_id, job, rows,
                source_entity, target_entity, chat_id,
            ),
            task_id=task_id, job_id=job_id, chat_id=chat_id,
        )
        bg = asyncio.create_task(wrapped)
        self._bg_tasks[task_id] = bg
        return bg

    def get_task(self, task_id):
        return self._bg_tasks.get(task_id)

    def remove_task(self, task_id):
        self._bg_tasks.pop(task_id, None)

    # -- error boundary -----------------------------------------------------

    async def _wrap_with_error_boundary(self, coro, task_id, job_id, chat_id):
        """Catch uncaught exceptions, mark job failed, notify user.
        CancelledError propagates without side effect."""
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Background job %s (task %s) crashed: %s",
                job_id, task_id, e, exc_info=True,
            )
            try:
                await self.agent.db.update_job_status(job_id, "failed")
            except Exception as db_err:
                logger.warning(
                    "Failed to mark job %s as failed: %s", job_id, db_err,
                )
            try:
                await self.agent.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.ERROR,
                    message=f"批量任務失敗：{e}",
                ))
            except Exception as ws_err:
                logger.warning(
                    "Failed to notify user of job %s failure: %s",
                    job_id, ws_err,
                )
            raise
        finally:
            self._bg_tasks.pop(task_id, None)
            self.agent._batch_message_cache.pop(job_id, None)

    # -- background coroutines (filled in Step 4) --------------------------

    async def _run_batch_background(
        self, task_id, job_id, job, source_entity, target_entity, chat_id,
    ):
        raise NotImplementedError(
            "Step 4: copy body from TGTransferAgent._run_batch_background"
        )

    async def _run_defer_scan_background(
        self, task_id, job_id, job, messages, chat_id,
    ):
        raise NotImplementedError(
            "Step 4: copy body from TGTransferAgent._run_defer_scan_background"
        )

    async def _run_process_deferred_background(
        self, task_id, job_id, job, rows, source_entity, target_entity, chat_id,
    ):
        raise NotImplementedError(
            "Step 4: copy body from TGTransferAgent._run_process_deferred_background"
        )
```

- [ ] **Step 4: Move the three coroutine bodies into BatchController**

Find the methods in `__main__.py`:

```bash
grep -n "async def _run_batch_background\|async def _run_defer_scan_background\|async def _run_process_deferred_background" agents/tg_transfer/__main__.py
```

For each method:

1. Copy the body (everything inside the method) into the corresponding placeholder in `batch_controller.py`.
2. Apply these textual replacements inside the copied body:
   - `self.engine` → `self.agent.engine`
   - `self.db` → `self.agent.db`
   - `self.media_db` → `self.agent.media_db`
   - `self.tg_client` → `self.agent.tg_client`
   - `self._pending_jobs` → `self.agent._pending_jobs`
   - `self._current_chat_id` → `self.agent._current_chat_id`
   - `self._bg_tasks` → `self._bg_tasks` (stays — controller owns it)
   - `self.ws_send_progress` → `self.agent.ws_send_progress`
   - `self.ws_send_result` → `self.agent.ws_send_result`
   - Calls to other agent methods like `self._skip_current_failed` → `self.agent._skip_current_failed`
3. **Remove** the inline `self._bg_tasks.pop(task_id, None)` calls in the original `finally` blocks — the wrapper's `finally` handles that.
4. **Remove** the inline `self._batch_message_cache.pop(job_id, None)` calls (same reason).

Example transformation for `_run_batch_background`'s typical structure:

```python
# BEFORE (in __main__.py):
async def _run_batch_background(self, task_id, job_id, job, source_entity, target_entity, chat_id):
    try:
        status = await self.engine.run_batch(job_id, source_entity, target_entity, report_fn)
        ...
    finally:
        if not keep_pending_binding:
            self._pending_jobs.pop(task_id, None)
        self._bg_tasks.pop(task_id, None)

# AFTER (in batch_controller.py):
async def _run_batch_background(self, task_id, job_id, job, source_entity, target_entity, chat_id):
    try:
        status = await self.agent.engine.run_batch(job_id, source_entity, target_entity, report_fn)
        ...
    finally:
        if not keep_pending_binding:
            self.agent._pending_jobs.pop(task_id, None)
        # _bg_tasks.pop is handled by the wrapper's finally
```

After moving each body, the `NotImplementedError` placeholder is gone.

- [ ] **Step 5: Update `__main__.py` to use BatchController**

In `agents/tg_transfer/__main__.py`:

1. Add import at top: `from agents.tg_transfer.batch_controller import BatchController`

2. In `__init__`, after `self._batch_message_cache: dict[str, list] = {}`, add:

```python
        self.batch_controller = BatchController(self)
```

3. **Delete** the three method definitions (`_run_batch_background`, `_run_defer_scan_background`, `_run_process_deferred_background`) from `TGTransferAgent` — they live in `BatchController` now.

4. **Delete** `_spawn_batch_bg` (the small spawner around line 122-134) — replaced by `self.batch_controller.spawn_batch(...)`.

5. **Delete** `self._bg_tasks: dict[str, asyncio.Task] = {}` from `__init__` — controller owns it.

6. Replace every reference globally:
   - `self._spawn_batch_bg(task_id, job_id, job, source_entity, target_entity, chat_id)` → `self.batch_controller.spawn_batch(task_id, job_id, job, source_entity, target_entity, chat_id)`
   - `asyncio.create_task(self._run_defer_scan_background(...))` → `self.batch_controller.spawn_defer_scan(...)`
   - `asyncio.create_task(self._run_process_deferred_background(...))` → `self.batch_controller.spawn_process_deferred(...)`
   - `self._bg_tasks.get(task_id)` → `self.batch_controller.get_task(task_id)`
   - `self._bg_tasks.pop(task_id, None)` → `self.batch_controller.remove_task(task_id)`
   - `self._bg_tasks[task_id] = bg` (manual assignment) → use spawn_* helpers; do NOT manually assign

Verify no leftover references:

```bash
grep -n "_bg_tasks\|_spawn_batch_bg\|_run_batch_background\|_run_defer_scan_background\|_run_process_deferred_background" agents/tg_transfer/__main__.py
```

Expected: zero matches.

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest tests/test_batch_controller.py -v 2>&1 | tail -10`

Expected: 2/2 PASS.

Run full suite:

Run: `PYTHONPATH=. pytest tests/ 2>&1 | tail -10`

Expected: 2 pre-existing failures, no new ones.

- [ ] **Step 7: Verify __main__.py shrunk**

Run: `wc -l agents/tg_transfer/__main__.py`

Expected: under 1100 lines (down from 1497).

- [ ] **Step 8: Commit**

```bash
git add agents/tg_transfer/batch_controller.py agents/tg_transfer/__main__.py tests/test_batch_controller.py
git commit -m "refactor(tg-transfer): extract BatchController + add error boundary"
```

---

## Task 7: Manual end-to-end verification + deployment

This task has no test code — it confirms the optimizations work on real infrastructure. Per project memory rule "修 bug 後務必 commit + build + deploy 一次完成".

- [ ] **Step 1: Build and restart**

Run: `docker compose up -d --build tg-transfer-agent`

Verify: `docker compose logs --tail=50 tg-transfer-agent | grep -iE "error|liveness"`

Expected: see `Liveness scan ... created with N media_ids`. No error tracebacks.

- [ ] **Step 2: Verify WAL mode active**

Run:
```bash
docker compose exec tg-transfer-agent python -c "
import sqlite3
conn = sqlite3.connect('/data/tg_transfer/transfer.db')
cur = conn.cursor()
cur.execute('PRAGMA journal_mode')
print('journal_mode:', cur.fetchone()[0])
"
```

Expected: `journal_mode: wal`

- [ ] **Step 3: Verify new indexes exist**

Run:
```bash
docker compose exec tg-transfer-agent python -c "
import sqlite3
conn = sqlite3.connect('/data/tg_transfer/transfer.db')
cur = conn.cursor()
cur.execute(\"SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_media%'\")
for r in cur.fetchall(): print(r[0])
"
```

Expected: list includes `idx_media_phash_lookup` and `idx_media_target_msg`.

- [ ] **Step 4: Re-batch test (verify dedup-from-media + caching wins)**

Send the agent a `/batch` for a small source you've fully transferred before. Watch logs:

```bash
docker compose logs -f tg-transfer-agent
```

Expected: `預計搬移：0` in the confirmation reply. After confirming, batch completes near-instantly with no thumb downloads.

- [ ] **Step 5: Fresh-batch test (verify single ffprobe + non-blocking hashes)**

Send a `/batch` for a small new source with at least one video. Watch logs:

- Should see at most 1 `ffprobe` call per video (not 2)
- WS heartbeats should remain regular through the transfer
- Job completes normally

- [ ] **Step 6: Error-boundary smoke test (verify F1)**

Trigger a known-bad scenario: e.g. `/batch` to a target chat the agent doesn't have permission for, OR an entity that doesn't resolve. Confirm:

- User receives an `ERROR` reply in TG
- Job in DB is `status='failed'`:

```bash
docker compose exec tg-transfer-agent python -c "
import sqlite3
conn = sqlite3.connect('/data/tg_transfer/transfer.db')
cur = conn.cursor()
cur.execute(\"SELECT job_id, status FROM jobs ORDER BY updated_at DESC LIMIT 3\")
for r in cur.fetchall(): print(r)
"
```

Expected: most-recent failed-batch row shows `status='failed'`.

- [ ] **Step 7: Document outcome**

If all steps pass, optimization deployment is verified. If any check fails, capture log + roll back via `git revert <range>`.

---

## Self-Review

**Spec coverage:**

| Spec item | Implementing task |
|---|---|
| A1 executemany | Task 1 step 4 |
| A2 WAL pragma | Task 1 steps 5,6 |
| A3 idx_media_phash_lookup | Task 1 step 6 |
| A4 idx_media_target_msg | Task 1 step 6 |
| B1 to_thread hashes | Task 2 step 5 |
| B2 single ffprobe | Task 2 steps 3,4 |
| C1 phash candidate cache | Task 3 steps 4,5 |
| C2 size_limit TTL | Task 3 step 3 |
| D1 search_keyword pagination | Task 4 step 5 |
| D2 batch_mark_failed_as_skipped | Task 4 steps 3,4 |
| E1 batch message cache | Task 5 step 3 |
| E2 liveness task ref + respawn | Task 5 step 4 |
| E3 album_window setting | Task 5 step 5 |
| F1 BatchController + error boundary | Task 6 |
| Deploy gate | Task 7 |

All 13 items mapped. No "TBD" or "implement later" placeholder text.

**Type/name consistency:**
- `phash_candidates` (list[dict] | None) is the kwarg used in `_transfer_media`, `transfer_single`, and `run_batch`'s `_get_candidates` helper.
- `_batch_message_cache`, `_liveness_task`, `_data_dir`, `_liveness_interval_seconds`, `_album_window`, `batch_controller` attribute names match between code and tests.
- BatchController API: `spawn_batch`, `spawn_defer_scan`, `spawn_process_deferred`, `get_task`, `remove_task`. Parameter names match callers in __main__.py.

Plan ready.
