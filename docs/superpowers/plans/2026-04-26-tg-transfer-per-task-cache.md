# TG Transfer Per-Task Download Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize tg_transfer download cache from flat `tmp/` into per-`task_id` subdirectories so deleting a hub conversation cleanly removes all associated downloads, with WebSocket push + orphan-scan fallback.

**Architecture:** Hub `handle_task_delete` emits a new `TASK_DELETED` WS message → tg_transfer agent removes `tmp/{task_id}/` and DB records. Agent startup also scans `tmp/` for directories with no active job and removes them, covering the offline-during-delete case. A one-shot migration on first launch flushes any legacy flat-layout files and resets `partial_path` rows.

**Tech Stack:** Python 3.12, aiosqlite, aiohttp WS, pytest + pytest-asyncio, Telethon (mocked in tests).

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `core/ws.py` | Modify | Add `MsgType.TASK_DELETED` enum value |
| `core/base_agent.py` | Modify | Dispatch `TASK_DELETED` WS message to `on_task_deleted` hook |
| `hub/dashboard.py` | Modify | `handle_task_delete` sends `TASK_DELETED` to bound agent before deleting hub rows |
| `agents/tg_transfer/db.py` | Modify | Add `delete_jobs_by_task`, `get_active_task_ids`, `clear_all_partials` helpers |
| `agents/tg_transfer/transfer_engine.py` | Modify | `transfer_album`, `transfer_single`, `_transfer_media` accept `task_id` and write under `tmp/{task_id}/` |
| `agents/tg_transfer/__main__.py` | Modify | Implement `on_task_deleted`, orphan scan in `on_ws_connected`, legacy migration in `__init__`, remove dead-branch comment in `_resume_batch`, pass `task_id` to engine in all call sites |
| `tests/test_db.py` | Modify | Tests for new DB helpers |
| `tests/test_transfer_engine.py` | Modify | Tests for per-task subdirectory paths |
| `tests/test_hub_server.py` | Modify | Test that `handle_task_delete` emits `TASK_DELETED` |
| `tests/test_tg_transfer_integration.py` | Modify | Test `on_task_deleted` cleanup + orphan scan + migration |

---

## Task 1: Add `TASK_DELETED` WS message type

**Files:**
- Modify: `core/ws.py`
- Modify: `core/base_agent.py`
- Test: `tests/test_base_agent.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_base_agent.py` (create file if absent — check first):

```python
import asyncio
import pytest
from core.base_agent import BaseAgent
from core.ws import MsgType, ws_msg


class _SubAgent(BaseAgent):
    def __init__(self):
        # Bypass BaseAgent.__init__ network bits for unit test
        self._cancelled_tasks = set()
        self.deleted = []

    def on_task_deleted(self, task_id: str):
        self.deleted.append(task_id)


@pytest.mark.asyncio
async def test_task_deleted_dispatches_to_hook():
    agent = _SubAgent()
    raw = ws_msg(MsgType.TASK_DELETED, task_id="abc-123")
    # Simulate the WS dispatch logic — the loop body must call on_task_deleted
    # for TASK_DELETED messages.
    import json
    data = json.loads(raw)
    if data.get("type") == MsgType.TASK_DELETED.value:
        agent.on_task_deleted(data["task_id"])
    assert agent.deleted == ["abc-123"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_base_agent.py::test_task_deleted_dispatches_to_hook -v`
Expected: FAIL with `AttributeError: TASK_DELETED` (enum value missing).

- [ ] **Step 3: Add `TASK_DELETED` to `MsgType`**

Edit `core/ws.py`:

```python
class MsgType(str, Enum):
    # Gateway → Hub
    DISPATCH = "dispatch"

    # Hub → Gateway
    REPLY = "reply"

    # Hub → Agent
    TASK = "task"
    CANCEL = "cancel"
    TASK_DELETED = "task_deleted"

    # Agent → Hub
    RESULT = "result"

    # Bidirectional (Agent → Hub → Gateway)
    PROGRESS = "progress"

    # Gateway → Hub (on connect)
    GW_REGISTER = "gw_register"
```

- [ ] **Step 4: Add `on_task_deleted` hook to `BaseAgent`**

Edit `core/base_agent.py`. Add the hook right after `on_cancel`:

```python
    def on_task_deleted(self, task_id: str):
        """Hook for subclasses to handle permanent task deletion. Called when
        the hub deletes a conversation — agent should release any task-scoped
        resources (cache directories, DB rows tied to this task)."""
        pass
```

Then in the WS dispatch loop (around the existing `elif msg_type == MsgType.CANCEL.value:` block), add a sibling branch:

```python
                                elif msg_type == MsgType.TASK_DELETED.value:
                                    task_id = data.get("task_id")
                                    if task_id:
                                        self.on_task_deleted(task_id)
                                        logger.info(f"Task deleted: {task_id}")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_base_agent.py::test_task_deleted_dispatches_to_hook -v`
Expected: PASS.

- [ ] **Step 6: Run full WS test suite to confirm no regression**

Run: `pytest tests/test_base_agent.py tests/test_dispatch.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add core/ws.py core/base_agent.py tests/test_base_agent.py
git commit -m "feat(core): add TASK_DELETED ws message and on_task_deleted hook"
```

---

## Task 2: Add `TransferDB` helpers for task-scoped deletion and active-task lookup

**Files:**
- Modify: `agents/tg_transfer/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
@pytest.mark.asyncio
async def test_delete_jobs_by_task_removes_jobs_and_messages(tmp_path):
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()
    job_id = await db.create_job(
        source_chat="s", target_chat="t", mode="batch", task_id="task-A",
    )
    await db.add_messages(job_id, [1, 2, 3])

    deleted = await db.delete_jobs_by_task("task-A")

    assert deleted == 1  # one job
    assert await db.get_job(job_id) is None
    # job_messages must also be gone
    assert await db.get_message(job_id, 1) is None
    await db.close()


@pytest.mark.asyncio
async def test_delete_jobs_by_task_unknown_task_returns_zero(tmp_path):
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()
    deleted = await db.delete_jobs_by_task("never-existed")
    assert deleted == 0
    await db.close()


@pytest.mark.asyncio
async def test_get_active_task_ids_filters_terminal(tmp_path):
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()
    j_running = await db.create_job(
        source_chat="s", target_chat="t", mode="batch", task_id="t-run",
    )
    await db.update_job_status(j_running, "running")
    j_paused = await db.create_job(
        source_chat="s", target_chat="t", mode="batch", task_id="t-paused",
    )
    await db.update_job_status(j_paused, "paused")
    j_done = await db.create_job(
        source_chat="s", target_chat="t", mode="batch", task_id="t-done",
    )
    await db.update_job_status(j_done, "completed")

    ids = await db.get_active_task_ids()

    assert "t-run" in ids
    assert "t-paused" in ids
    assert "t-done" not in ids
    await db.close()


@pytest.mark.asyncio
async def test_clear_all_partials_resets_every_partial(tmp_path):
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()
    j = await db.create_job(
        source_chat="s", target_chat="t", mode="batch", task_id="x",
    )
    await db.add_messages(j, [10, 20])
    await db.set_partial(j, 10, "/some/path", 1024)
    await db.set_partial(j, 20, "/other/path", 2048)

    rows = await db.clear_all_partials()

    assert rows == 2
    msg10 = await db.get_message(j, 10)
    msg20 = await db.get_message(j, 20)
    assert msg10["partial_path"] is None
    assert msg10["downloaded_bytes"] == 0
    assert msg20["partial_path"] is None
    assert msg20["downloaded_bytes"] == 0
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::test_delete_jobs_by_task_removes_jobs_and_messages tests/test_db.py::test_delete_jobs_by_task_unknown_task_returns_zero tests/test_db.py::test_get_active_task_ids_filters_terminal tests/test_db.py::test_clear_all_partials_resets_every_partial -v`
Expected: FAIL with `AttributeError: 'TransferDB' object has no attribute 'delete_jobs_by_task'` etc.

- [ ] **Step 3: Implement helpers in `agents/tg_transfer/db.py`**

Add inside class `TransferDB`, right after `update_job_binding` (the `# -- Messages --` section follows):

```python
    async def delete_jobs_by_task(self, task_id: str) -> int:
        """Delete every job (and its job_messages) bound to `task_id`.
        Returns number of `jobs` rows removed.

        Used when the hub deletes a conversation: the agent must release the
        task's DB rows so resume scans don't try to revive them, and the
        per-task cache directory has no orphan jobs pointing at it."""
        async with self._db.execute(
            "SELECT job_id FROM jobs WHERE task_id = ?", (task_id,),
        ) as cur:
            job_ids = [row["job_id"] for row in await cur.fetchall()]
        for jid in job_ids:
            await self._db.execute(
                "DELETE FROM job_messages WHERE job_id = ?", (jid,),
            )
            await self._db.execute(
                "DELETE FROM jobs WHERE job_id = ?", (jid,),
            )
        await self._db.commit()
        return len(job_ids)

    async def get_active_task_ids(self) -> set[str]:
        """All task_ids tied to jobs in non-terminal status. Used by the
        orphan-scan fallback: any tmp/{task_id}/ directory whose task_id is
        NOT in this set was already abandoned and can be removed."""
        async with self._db.execute(
            "SELECT DISTINCT task_id FROM jobs "
            "WHERE task_id IS NOT NULL AND status NOT IN "
            "('completed', 'failed', 'cancelled')"
        ) as cur:
            return {row["task_id"] for row in await cur.fetchall()}

    async def clear_all_partials(self) -> int:
        """Reset every job_messages.partial_path / downloaded_bytes.
        Used during the legacy-layout migration: old absolute paths point at
        the flat tmp/ layout that no longer exists, so we force a clean
        re-download on the next attempt. Returns row count touched."""
        cur = await self._db.execute(
            "UPDATE job_messages SET partial_path = NULL, downloaded_bytes = 0 "
            "WHERE partial_path IS NOT NULL",
        )
        await self._db.commit()
        return cur.rowcount or 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: all PASS (including the four new ones).

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/db.py tests/test_db.py
git commit -m "feat(tg-transfer): add task-scoped DB helpers for cache lifecycle"
```

---

## Task 3: `TransferEngine` writes downloads under per-task subdirectory

**Files:**
- Modify: `agents/tg_transfer/transfer_engine.py:399-636` (`transfer_album`)
- Modify: `agents/tg_transfer/transfer_engine.py:725-925` (`_transfer_media`)
- Modify: `agents/tg_transfer/transfer_engine.py:375-397` (`transfer_single`)
- Test: `tests/test_transfer_engine.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transfer_engine.py` (read existing imports first to match patterns; tests likely already use a fake `client` and `TransferDB`):

```python
@pytest.mark.asyncio
async def test_transfer_album_writes_into_task_subdir(tmp_path, monkeypatch):
    """transfer_album with task_id must mkdir tmp/{task_id}/ and write
    downloaded files there, not into the flat tmp_dir root."""
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()
    captured_paths = []

    class FakeClient:
        async def download_media(self, msg, file):
            captured_paths.append(file)
            # Touch the file so atomicity check passes
            with open(file, "wb") as f:
                f.write(b"x" * 16)
            return file

    engine = TransferEngine(
        client=FakeClient(), db=db,
        tmp_dir=str(tmp_path / "tmp"),
    )

    class FakeMsg:
        def __init__(self, mid):
            self.id = mid
            self.text = ""
            self.media = object()
            self.file = type("F", (), {"size": 16, "name": None, "ext": ".jpg"})()

    # Skip the actual upload — for this test we just need to confirm path
    # construction. Patch the upload to no-op success.
    async def _noop_upload(*args, **kwargs):
        return [type("M", (), {"id": 1})()]
    monkeypatch.setattr(engine, "_upload_album_manual", _noop_upload)
    monkeypatch.setattr(engine, "should_skip", lambda _m: False)
    monkeypatch.setattr(engine, "_detect_file_type", lambda _m: "photo")
    # Bypass per-file dedup classification for this path test
    monkeypatch.setattr(
        "agents.tg_transfer.transfer_engine.classify_phash_dedup",
        lambda **_kw: ("upload", None, (0, 0)),
    )

    msgs = [FakeMsg(101), FakeMsg(102)]
    ok = await engine.transfer_album(
        target_entity=None, messages=msgs,
        target_chat="t", source_chat="s", task_id="task-XYZ",
    )

    assert ok is True
    expected_dir = os.path.join(str(tmp_path / "tmp"), "task-XYZ")
    for p in captured_paths:
        assert p.startswith(expected_dir + os.sep), f"{p} not under {expected_dir}"
    await db.close()


@pytest.mark.asyncio
async def test_transfer_media_writes_into_task_subdir(tmp_path, monkeypatch):
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()

    class FakeClient:
        async def download_media(self, msg, file):
            with open(file, "wb") as f:
                f.write(b"x" * 16)
            return file

    engine = TransferEngine(
        client=FakeClient(), db=db,
        tmp_dir=str(tmp_path / "tmp"),
    )

    class FakeMsg:
        id = 555
        text = ""
        media = object()
        file = type("F", (), {"size": 16, "name": None, "ext": ".jpg"})()

    monkeypatch.setattr(engine, "should_skip", lambda _m: False)
    monkeypatch.setattr(engine, "_detect_file_type", lambda _m: "photo")
    # Skip pre-dedup so we don't need a media_db
    result = await engine._transfer_media(
        target_entity=None, message=FakeMsg(),
        target_chat="t", source_chat="s",
        job_id=None, skip_pre_dedup=True,
        task_id="task-ABC",
    )

    expected_dir = os.path.join(str(tmp_path / "tmp"), "task-ABC")
    # Even if upload was no-op, the download path should have been under
    # the task subdir. Check that the directory exists (mkdir was called).
    assert os.path.isdir(expected_dir)
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_transfer_engine.py::test_transfer_album_writes_into_task_subdir tests/test_transfer_engine.py::test_transfer_media_writes_into_task_subdir -v`
Expected: FAIL — `transfer_album` does not accept `task_id`, `_transfer_media` does not accept `task_id`.

- [ ] **Step 3: Add `task_id` parameter to `transfer_album` and use task subdir**

Edit `agents/tg_transfer/transfer_engine.py`. Locate the `transfer_album` signature at line ~399 and modify:

```python
    async def transfer_album(self, target_entity, messages: list,
                              target_chat: str = "", source_chat: str = "",
                              job_id: str = None,
                              task_id: str = None) -> bool:
        """Transfer a media group (album) as a single album.
        Atomic: if any download fails, nothing is uploaded.

        `task_id`: hub conversation id. When provided, downloads land in
        tmp/{task_id}/ so deleting the hub task cleans them in one rmtree.
        Falls back to flat tmp_dir for direct/CLI invocations without a task.

        Also writes one media_db row per file (status=uploaded on success) so
        dashboard stats reflect real transfer count.
        """
        task_dir = (
            os.path.join(self.tmp_dir, task_id) if task_id else self.tmp_dir
        )
        os.makedirs(task_dir, exist_ok=True)
```

Then replace every `os.path.join(self.tmp_dir, ...)` inside `transfer_album` with `os.path.join(task_dir, ...)`. There are at least these spots (line numbers approximate, verify with `grep -n "self.tmp_dir" agents/tg_transfer/transfer_engine.py`):

- Around line 424 (inside `for msg in messages`): `dest = os.path.join(task_dir, f"{base}{ext}")`
- Around line 486 (`compute_phash_video(path, self.tmp_dir)`): change to `compute_phash_video(path, task_dir)` — keeps the per-task subdir for ffmpeg frame extraction
- Around line 569-570 (`thumb_dest = os.path.join(self.tmp_dir, ...)`): change to `os.path.join(task_dir, ...)`

- [ ] **Step 4: Add `task_id` parameter to `_transfer_media` and `transfer_single`**

Modify `_transfer_media` signature (line ~725):

```python
    async def _transfer_media(self, target_entity, message, target_chat: str = "",
                               source_chat: str = "", job_id: str = None,
                               skip_pre_dedup: bool = False,
                               task_id: str = None) -> dict:
        """Download and re-upload a single media message.
        Returns: {"ok": bool, "dedup": bool, "similar": list | None}

        `task_id`: hub conversation id; downloads go under tmp/{task_id}/.
        """
        task_dir = (
            os.path.join(self.tmp_dir, task_id) if task_id else self.tmp_dir
        )
        os.makedirs(task_dir, exist_ok=True)
```

Then inside `_transfer_media`, replace:
- `media_path = os.path.join(self.tmp_dir, f"{base}{ext}")` → `media_path = os.path.join(task_dir, f"{base}{ext}")`
- `thumb_path_target = os.path.join(self.tmp_dir, f"{base}.thumb.jpg")` → `os.path.join(task_dir, ...)`
- `compute_phash_video(path, self.tmp_dir)` → `compute_phash_video(path, task_dir)`

Modify `transfer_single` (line ~375) to forward `task_id`:

```python
    async def transfer_single(self, source_entity, target_entity, message,
                               target_chat: str = "", source_chat: str = "",
                               job_id: str = None,
                               skip_pre_dedup: bool = False,
                               task_id: str = None) -> dict:
        """Transfer a single message. Returns {"ok": bool, "dedup": bool, "similar": list | None}.

        `skip_pre_dedup`: bypass Phase 4 thumb-phash check. Used by the Phase 5
        dedup resolver when the user explicitly marks an ambiguous source as
        "different" — otherwise we'd just re-park it in pending_dedup forever.
        `task_id`: hub conversation id; downloads go under tmp/{task_id}/.
        """
        if message.media and not self.should_skip(message):
            return await self._transfer_media(
                target_entity, message, target_chat=target_chat,
                source_chat=source_chat, job_id=job_id,
                skip_pre_dedup=skip_pre_dedup,
                task_id=task_id,
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_transfer_engine.py -v`
Expected: all PASS — both new tests + the existing 412-line test suite (regression check).

- [ ] **Step 6: Commit**

```bash
git add agents/tg_transfer/transfer_engine.py tests/test_transfer_engine.py
git commit -m "feat(tg-transfer): write downloads under tmp/{task_id}/ subdirectory"
```

---

## Task 4: Plumb `task_id` from agent call sites into engine

**Files:**
- Modify: `agents/tg_transfer/transfer_engine.py:1048-1177` (`run_batch` — internal `transfer_album`/`transfer_single` calls)
- Modify: `agents/tg_transfer/__main__.py:364, 369, 1076, 1206` (call sites)

- [ ] **Step 1: Plumb `task_id` through `run_batch`**

`run_batch` already has `job_id` and reads `job` via `self.db.get_job(job_id)`. Modify both internal calls so they pass `task_id=job.get("task_id")` to `transfer_album` (line ~1102) and `transfer_single` (line ~1152). Concretely:

```python
                            ok = await self.transfer_album(
                                target_entity, messages,
                                target_chat=job["target_chat"],
                                source_chat=job["source_chat"],
                                job_id=job_id,
                                task_id=job.get("task_id"),
                            )
```

```python
                        result = await self.transfer_single(
                            source_entity, target_entity, msg,
                            target_chat=job["target_chat"],
                            source_chat=job["source_chat"],
                            job_id=job_id,
                            task_id=job.get("task_id"),
                        )
```

- [ ] **Step 2: Plumb `task_id` from `__main__.py` direct call sites**

Locate the four call sites (line numbers from `grep -n 'self.engine.transfer' agents/tg_transfer/__main__.py`) and add `task_id=`:

- Line ~364 (`_handle_single` album branch):
  ```python
  ok = await self.engine.transfer_album(
      target_entity, album_msgs,
      task_id=task.task_id,
  )
  ```
  This requires the enclosing function to have `task` in scope. Verify by reading the function header — it's `_handle_single(self, task: TaskRequest, chat_id, message_id)`. ✓

- Line ~369 (`_handle_single` single branch):
  ```python
  result = await self.engine.transfer_single(
      source_entity, target_entity, msg,
      target_chat=target_chat, source_chat=str(chat_id), job_id=None,
      task_id=task.task_id,
  )
  ```

- Line ~1076 (`_run_process_deferred_background`):
  ```python
  result = await self.engine.transfer_single(
      source_entity, target_entity, msg,
      target_chat=job["target_chat"],
      source_chat=job["source_chat"],
      job_id=job_id,
      skip_pre_dedup=True,
      task_id=task_id,
  )
  ```
  Confirm `task_id` is in scope by reading the function signature `_run_process_deferred_background(self, task_id, job_id, ...)`. ✓

- Line ~1206 (`_handle_dedup_response`):
  ```python
  result = await self.engine.transfer_single(
      source_entity, target_entity, msg,
      target_chat=job["target_chat"],
      source_chat=job["source_chat"],
      job_id=job_id,
      skip_pre_dedup=True,
      task_id=task.task_id,
  )
  ```

- [ ] **Step 3: Run the integration test suite to confirm nothing broke**

Run: `pytest tests/test_transfer_engine.py tests/test_tg_transfer_integration.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add agents/tg_transfer/transfer_engine.py agents/tg_transfer/__main__.py
git commit -m "feat(tg-transfer): pass task_id through all transfer call sites"
```

---

## Task 5: Hub emits `TASK_DELETED` to bound agent on conversation delete

**Files:**
- Modify: `hub/dashboard.py:475-490` (`handle_task_delete`)
- Test: `tests/test_hub_server.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_hub_server.py`. Read existing test patterns first — there's likely a fixture for a running hub app + a fake agent WS. Match it. If patterns are fully custom, here's the gist:

```python
@pytest.mark.asyncio
async def test_handle_task_delete_sends_task_deleted_to_agent(hub_app, fake_agent_ws):
    """Deleting a conversation must push TASK_DELETED to the bound agent
    so the agent can rmtree its task-scoped cache and DB rows."""
    tm = hub_app["task_manager"]
    task = tm.create_task(
        agent_name="tg_transfer", chat_id=12345, content="batch x to y",
    )
    hub_app["registry"].set_ws("tg_transfer", fake_agent_ws)

    async with await hub_app_client(hub_app) as client:
        resp = await client.post(f"/dashboard/task/{task['task_id']}/delete")
        assert resp.status == 200

    # fake_agent_ws should have received a TASK_DELETED frame
    sent = fake_agent_ws.sent_messages
    decoded = [json.loads(s) for s in sent]
    assert any(
        m.get("type") == "task_deleted" and m.get("task_id") == task["task_id"]
        for m in decoded
    ), f"TASK_DELETED not in sent: {decoded}"
```

If `tests/test_hub_server.py` doesn't have `hub_app` / `fake_agent_ws` fixtures, the simpler form is:

```python
@pytest.mark.asyncio
async def test_handle_task_delete_sends_task_deleted_to_agent(tmp_path):
    from hub.task_manager import TaskManager
    from hub.dashboard import handle_task_delete
    from aiohttp import web

    tm = TaskManager(str(tmp_path / "tasks.db"))
    task = tm.create_task(
        agent_name="tg_transfer", chat_id=12345, content="batch x to y",
    )

    sent = []

    class FakeWS:
        async def send_str(self, s):
            sent.append(s)

    class FakeRegistry:
        def get_ws(self, name):
            return FakeWS()

    app = web.Application()
    app["task_manager"] = tm
    app["registry"] = FakeRegistry()

    request = make_mocked_request(
        "POST", f"/dashboard/task/{task['task_id']}/delete",
        match_info={"task_id": task["task_id"]},
        app=app,
    )
    resp = await handle_task_delete(request)
    assert resp.status == 200

    decoded = [json.loads(s) for s in sent]
    assert any(
        m.get("type") == "task_deleted" and m.get("task_id") == task["task_id"]
        for m in decoded
    )
```

(`make_mocked_request` is from `aiohttp.test_utils`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_hub_server.py::test_handle_task_delete_sends_task_deleted_to_agent -v`
Expected: FAIL — only the existing CANCEL is sent, no TASK_DELETED.

- [ ] **Step 3: Update `handle_task_delete` to emit `TASK_DELETED`**

Edit `hub/dashboard.py` around line 475-490:

```python
async def handle_task_delete(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    tm = request.app["task_manager"]
    task = tm.get_task(task_id)

    registry = request.app["registry"]
    if task:
        agent_ws = registry.get_ws(task["agent_name"])
        if agent_ws:
            from core.ws import ws_msg, MsgType
            # CANCEL only for in-flight states (matches handle_task_close
            # semantics — stop work in progress).
            if task["status"] in ("working", "waiting_input", "waiting_approval"):
                await agent_ws.send_str(ws_msg(MsgType.CANCEL, task_id=task_id))
            # TASK_DELETED unconditionally — agent must release task-scoped
            # resources (cache dir, DB rows) regardless of state. Offline
            # agents miss this; orphan scan on next startup catches up.
            await agent_ws.send_str(
                ws_msg(MsgType.TASK_DELETED, task_id=task_id),
            )

    tm._conn.execute("DELETE FROM task_messages WHERE task_id = ?", (task_id,))
    tm._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    tm._conn.commit()
    return web.json_response({"status": "ok"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_hub_server.py::test_handle_task_delete_sends_task_deleted_to_agent -v`
Expected: PASS.

- [ ] **Step 5: Run hub test suite to confirm no regression**

Run: `pytest tests/test_hub_server.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add hub/dashboard.py tests/test_hub_server.py
git commit -m "feat(hub): emit TASK_DELETED to agent on conversation delete"
```

---

## Task 6: Implement `on_task_deleted` in tg_transfer agent

**Files:**
- Modify: `agents/tg_transfer/__main__.py` (around the existing `on_cancel` definition at line ~232)
- Test: `tests/test_tg_transfer_integration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tg_transfer_integration.py`:

```python
@pytest.mark.asyncio
async def test_on_task_deleted_removes_dir_and_db_rows(tmp_path):
    """Calling on_task_deleted must rmtree tmp/{task_id}/ and delete the
    task's jobs + job_messages, plus drop in-memory state."""
    import shutil
    from agents.tg_transfer.__main__ import TgTransferAgent

    # Set up: real TransferDB with one bound job, and a tmp/{task_id}/ dir
    # containing a fake artefact.
    agent = await _build_test_agent(tmp_path)  # helper; see Step 3
    job_id = await agent.db.create_job(
        source_chat="s", target_chat="t", mode="batch", task_id="task-DEL",
    )
    await agent.db.add_messages(job_id, [1])

    task_dir = os.path.join(agent.engine.tmp_dir, "task-DEL")
    os.makedirs(task_dir, exist_ok=True)
    with open(os.path.join(task_dir, "leftover.mp4"), "wb") as f:
        f.write(b"x")

    agent._pending_jobs["task-DEL"] = job_id
    agent._current_chat_id["task-DEL"] = 999

    await agent._on_task_deleted_async("task-DEL")

    # Directory gone
    assert not os.path.exists(task_dir)
    # DB rows gone
    assert await agent.db.get_job(job_id) is None
    # In-memory binding gone
    assert "task-DEL" not in agent._pending_jobs
    assert "task-DEL" not in agent._current_chat_id
```

The `_build_test_agent` helper depends on patterns already in `tests/test_tg_transfer_integration.py`. Read the existing fixtures and reuse them. If none exists, add a minimal one above the test:

```python
async def _build_test_agent(tmp_path):
    """Bare-minimum TgTransferAgent for unit tests: real DB + engine,
    fake TG client, no WS, no Hub."""
    from agents.tg_transfer.__main__ import TgTransferAgent
    from agents.tg_transfer.db import TransferDB
    from agents.tg_transfer.transfer_engine import TransferEngine

    agent = TgTransferAgent.__new__(TgTransferAgent)
    agent._pending_jobs = {}
    agent._bg_tasks = {}
    agent._current_chat_id = {}
    agent._search_state = {}
    agent._awaiting_target = {}
    agent._cancelled_tasks = set()
    agent.db = TransferDB(str(tmp_path / "t.db"))
    await agent.db.init()
    agent.engine = TransferEngine(
        client=None, db=agent.db, tmp_dir=str(tmp_path / "tmp"),
    )
    return agent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tg_transfer_integration.py::test_on_task_deleted_removes_dir_and_db_rows -v`
Expected: FAIL — `on_task_deleted` (and `_on_task_deleted_async`) not implemented.

- [ ] **Step 3: Implement `on_task_deleted` and helper**

Edit `agents/tg_transfer/__main__.py`. Add an import at the top if not present:

```python
import shutil
```

Add right after `on_cancel` (line ~232):

```python
    def on_task_deleted(self, task_id: str):
        """Hub deleted this conversation. Schedule async cleanup of the
        task-scoped cache directory and DB rows. Synchronous-from-WS-loop
        wrapper around _on_task_deleted_async."""
        asyncio.create_task(self._on_task_deleted_async(task_id))

    async def _on_task_deleted_async(self, task_id: str):
        # Cancel any live background coroutine first so it doesn't write
        # into a directory we're about to remove.
        bg = self._bg_tasks.pop(task_id, None)
        if bg is not None and not bg.done():
            bg.cancel()
        # Mark cancelled so any in-flight engine.run_batch loop bails out.
        # Look up the job_id for this task so engine.cancel_job is keyed
        # correctly (engine cancels by job_id, not task_id).
        job_id = self._pending_jobs.pop(task_id, None)
        if job_id:
            self.engine.cancel_job(job_id)

        # Drop other in-memory bindings.
        self._current_chat_id.pop(task_id, None)
        self._search_state.pop(task_id, None)
        self._awaiting_target.pop(task_id, None)

        # Remove DB rows. Errors here are non-fatal; orphan scan on next
        # startup will retry.
        try:
            await self.db.delete_jobs_by_task(task_id)
        except Exception as e:
            logger.warning(f"delete_jobs_by_task({task_id}) failed: {e}")

        # Remove the per-task cache directory.
        task_dir = os.path.join(self.engine.tmp_dir, task_id)
        try:
            shutil.rmtree(task_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"rmtree({task_dir}) failed: {e}")

        logger.info(f"Cleaned up cache + DB for deleted task {task_id}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tg_transfer_integration.py::test_on_task_deleted_removes_dir_and_db_rows -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/__main__.py tests/test_tg_transfer_integration.py
git commit -m "feat(tg-transfer): implement on_task_deleted hook"
```

---

## Task 7: Orphan scan on agent startup

**Files:**
- Modify: `agents/tg_transfer/__main__.py:99-110` (`on_ws_connected`)
- Test: `tests/test_tg_transfer_integration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tg_transfer_integration.py`:

```python
@pytest.mark.asyncio
async def test_orphan_scan_removes_dir_with_no_active_job(tmp_path):
    """tmp/{task_id}/ directories whose task_id has no active job in the
    DB must be removed on startup (covers the agent-was-offline-when-hub-
    deleted case)."""
    agent = await _build_test_agent(tmp_path)

    # Active job — its dir should survive
    active_job = await agent.db.create_job(
        source_chat="s", target_chat="t", mode="batch", task_id="active",
    )
    await agent.db.update_job_status(active_job, "running")
    os.makedirs(os.path.join(agent.engine.tmp_dir, "active"), exist_ok=True)

    # Completed job's task_id — orphan, dir should go
    done_job = await agent.db.create_job(
        source_chat="s", target_chat="t", mode="batch", task_id="done",
    )
    await agent.db.update_job_status(done_job, "completed")
    os.makedirs(os.path.join(agent.engine.tmp_dir, "done"), exist_ok=True)

    # Wholly unknown task_id — orphan, dir should go
    os.makedirs(os.path.join(agent.engine.tmp_dir, "stranger"), exist_ok=True)

    await agent._scan_orphan_task_dirs()

    assert os.path.isdir(os.path.join(agent.engine.tmp_dir, "active"))
    assert not os.path.exists(os.path.join(agent.engine.tmp_dir, "done"))
    assert not os.path.exists(os.path.join(agent.engine.tmp_dir, "stranger"))


@pytest.mark.asyncio
async def test_orphan_scan_ignores_non_directory_entries(tmp_path):
    """If something weird is sitting in tmp_dir root (legacy file, dotfile),
    the orphan scan must not crash. It should ignore non-directories."""
    agent = await _build_test_agent(tmp_path)
    os.makedirs(agent.engine.tmp_dir, exist_ok=True)
    # Stray legacy file
    with open(os.path.join(agent.engine.tmp_dir, "legacy.mp4"), "wb") as f:
        f.write(b"x")
    # Stray dotfile (e.g. .DS_Store, migration flag)
    with open(os.path.join(agent.engine.tmp_dir, ".something"), "wb") as f:
        f.write(b"")

    await agent._scan_orphan_task_dirs()  # must not raise

    # Files should still be there — orphan scan is per-directory only
    assert os.path.exists(os.path.join(agent.engine.tmp_dir, "legacy.mp4"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tg_transfer_integration.py::test_orphan_scan_removes_dir_with_no_active_job tests/test_tg_transfer_integration.py::test_orphan_scan_ignores_non_directory_entries -v`
Expected: FAIL — `_scan_orphan_task_dirs` not implemented.

- [ ] **Step 3: Implement `_scan_orphan_task_dirs` and wire it into `on_ws_connected`**

Edit `agents/tg_transfer/__main__.py`. Add this method on the agent class (placement: near the other `_resume*` methods around line 160-180):

```python
    async def _scan_orphan_task_dirs(self):
        """Remove tmp/{task_id}/ directories whose task_id has no active job.

        Rationale: hub may have deleted a conversation while this agent was
        offline, so we never received TASK_DELETED. On startup, anything
        not pointed at by an active job in the DB is dead weight — clear it.

        Only operates on direct subdirectories of tmp_dir. Ignores files at
        the root level (those are handled by the legacy migration in
        Task 8) and dotfiles (e.g. .migrated_v2 flag)."""
        tmp_root = self.engine.tmp_dir
        if not os.path.isdir(tmp_root):
            return
        try:
            active_ids = await self.db.get_active_task_ids()
        except Exception as e:
            logger.warning(f"get_active_task_ids failed during orphan scan: {e}")
            return
        for entry in os.listdir(tmp_root):
            if entry.startswith("."):
                continue
            full = os.path.join(tmp_root, entry)
            if not os.path.isdir(full):
                continue
            if entry in active_ids:
                continue
            try:
                shutil.rmtree(full, ignore_errors=True)
                logger.info(f"Orphan scan removed {full}")
            except Exception as e:
                logger.warning(f"Orphan scan failed to remove {full}: {e}")
```

Wire it into `on_ws_connected` (line ~99). Run after `_resume_interrupted_jobs` so resume sees the live state first:

```python
    async def on_ws_connected(self):
        """Resume interrupted jobs on every WS connect.

        Runs every time (not just first connect) so hub restarts or WS flaps
        that silently killed a `_run_batch_background` coroutine can recover.
        Re-spawn is guarded by a liveness check on the tracked asyncio.Task so
        healthy jobs aren't double-spawned. Paused-job user reminders only fire
        on the first connect to avoid spamming on every reconnect.

        Orphan scan runs once on first connect — it's idempotent but only
        useful when the agent has just (re)started.
        """
        first_connect = not getattr(self, '_resumed', False)
        self._resumed = True
        await self._resume_interrupted_jobs(first_connect=first_connect)
        if first_connect:
            await self._scan_orphan_task_dirs()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tg_transfer_integration.py::test_orphan_scan_removes_dir_with_no_active_job tests/test_tg_transfer_integration.py::test_orphan_scan_ignores_non_directory_entries -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/__main__.py tests/test_tg_transfer_integration.py
git commit -m "feat(tg-transfer): orphan-scan tmp/ on startup for offline-deleted tasks"
```

---

## Task 8: One-shot legacy-layout migration on startup

**Files:**
- Modify: `agents/tg_transfer/__main__.py` (called from `__init__` or `on_ws_connected` startup phase — see Step 3)
- Test: `tests/test_tg_transfer_integration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tg_transfer_integration.py`:

```python
@pytest.mark.asyncio
async def test_legacy_migration_clears_root_files_and_partials(tmp_path):
    """First startup with the new layout must:
    - delete every file at the root of tmp/ (the old flat layout)
    - reset every partial_path/downloaded_bytes in job_messages
    - write the .migrated_v2 flag so it doesn't re-run."""
    agent = await _build_test_agent(tmp_path)
    os.makedirs(agent.engine.tmp_dir, exist_ok=True)

    # Old flat-layout artefacts
    legacy_a = os.path.join(agent.engine.tmp_dir, "32171_4e0a9e2d.mp4")
    legacy_b = os.path.join(agent.engine.tmp_dir, "32172_d8ca1764.mp4")
    with open(legacy_a, "wb") as f:
        f.write(b"x" * 100)
    with open(legacy_b, "wb") as f:
        f.write(b"x" * 100)

    # An already-correct subdir — must NOT be touched
    keep_dir = os.path.join(agent.engine.tmp_dir, "task-keep")
    os.makedirs(keep_dir, exist_ok=True)
    with open(os.path.join(keep_dir, "y.bin"), "wb") as f:
        f.write(b"y")

    # Existing partial in DB (legacy absolute path)
    job_id = await agent.db.create_job(
        source_chat="s", target_chat="t", mode="batch", task_id="task-keep",
    )
    await agent.db.add_messages(job_id, [42])
    await agent.db.set_partial(job_id, 42, legacy_a, 100)

    await agent._migrate_legacy_tmp_layout()

    # Root-level files removed
    assert not os.path.exists(legacy_a)
    assert not os.path.exists(legacy_b)
    # Subdir survives
    assert os.path.exists(os.path.join(keep_dir, "y.bin"))
    # Partial reset
    msg = await agent.db.get_message(job_id, 42)
    assert msg["partial_path"] is None
    assert msg["downloaded_bytes"] == 0
    # Flag written
    assert os.path.exists(
        os.path.join(agent.engine.tmp_dir, ".migrated_v2"),
    )


@pytest.mark.asyncio
async def test_legacy_migration_idempotent_when_flag_present(tmp_path):
    """If .migrated_v2 is present, migration must be a no-op even when
    root-level files exist (those would now be from a different cause and
    should not be silently nuked)."""
    agent = await _build_test_agent(tmp_path)
    os.makedirs(agent.engine.tmp_dir, exist_ok=True)
    flag = os.path.join(agent.engine.tmp_dir, ".migrated_v2")
    with open(flag, "wb") as f:
        f.write(b"")

    sentinel = os.path.join(agent.engine.tmp_dir, "post_migration.bin")
    with open(sentinel, "wb") as f:
        f.write(b"x")

    await agent._migrate_legacy_tmp_layout()

    assert os.path.exists(sentinel)  # untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tg_transfer_integration.py::test_legacy_migration_clears_root_files_and_partials tests/test_tg_transfer_integration.py::test_legacy_migration_idempotent_when_flag_present -v`
Expected: FAIL — method not implemented.

- [ ] **Step 3: Implement `_migrate_legacy_tmp_layout` and call it on startup**

Add to `agents/tg_transfer/__main__.py` (place near `_scan_orphan_task_dirs`):

```python
    async def _migrate_legacy_tmp_layout(self):
        """One-shot migration from the flat tmp/ layout to per-task subdirs.

        Pre-Task-3, downloads landed directly in tmp_dir as `{msg}_{uuid}.ext`.
        After Task 3, every file is under `tmp/{task_id}/`. Root-level files
        from the old layout can't be reliably re-attributed to a task, so we:

        1. Remove every regular file at the root of tmp_dir.
        2. Reset all job_messages.partial_path / downloaded_bytes — those
           rows referenced absolute paths under the flat layout that we just
           wiped, so resume must re-download from byte 0.
        3. Drop a `.migrated_v2` flag file so subsequent boots skip this.

        Subdirectories are left alone — they're either pre-existing manual
        creations (rare) or the new per-task layout (after a partial deploy).
        """
        tmp_root = self.engine.tmp_dir
        if not os.path.isdir(tmp_root):
            os.makedirs(tmp_root, exist_ok=True)
        flag = os.path.join(tmp_root, ".migrated_v2")
        if os.path.exists(flag):
            return

        removed = 0
        for entry in os.listdir(tmp_root):
            if entry.startswith("."):
                continue
            full = os.path.join(tmp_root, entry)
            if os.path.isfile(full):
                try:
                    os.remove(full)
                    removed += 1
                except Exception as e:
                    logger.warning(f"Legacy migration: failed to remove {full}: {e}")

        try:
            reset_rows = await self.db.clear_all_partials()
        except Exception as e:
            logger.warning(f"Legacy migration: clear_all_partials failed: {e}")
            reset_rows = 0

        try:
            with open(flag, "w") as f:
                f.write("v2\n")
        except Exception as e:
            logger.warning(f"Legacy migration: failed to write flag: {e}")

        logger.info(
            f"Legacy migration done: removed {removed} root-level files, "
            f"reset {reset_rows} partial-download rows"
        )
```

Wire it into agent startup. Locate where `self.engine` is constructed in `__main__.py` (around line 81-90 in `__init__` of the agent — verify by reading that block). Migration must run AFTER `engine` exists but BEFORE any new job activity. The cleanest hook is `on_ws_connected` first-connect path, before `_resume_interrupted_jobs`:

```python
    async def on_ws_connected(self):
        first_connect = not getattr(self, '_resumed', False)
        self._resumed = True
        if first_connect:
            await self._migrate_legacy_tmp_layout()
        await self._resume_interrupted_jobs(first_connect=first_connect)
        if first_connect:
            await self._scan_orphan_task_dirs()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tg_transfer_integration.py::test_legacy_migration_clears_root_files_and_partials tests/test_tg_transfer_integration.py::test_legacy_migration_idempotent_when_flag_present -v`
Expected: PASS.

- [ ] **Step 5: Run full integration suite to confirm no regression**

Run: `pytest tests/test_tg_transfer_integration.py tests/test_db.py tests/test_transfer_engine.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add agents/tg_transfer/__main__.py tests/test_tg_transfer_integration.py
git commit -m "feat(tg-transfer): one-shot legacy tmp/ flat-layout migration"
```

---

## Task 9: Clean up dead-branch comment in `_resume_batch`

**Files:**
- Modify: `agents/tg_transfer/__main__.py:1248-1251`

This is documentation hygiene — the conditional `job.get("task_id") != task_id` is unreachable given the `_pending_jobs` invariant established in the spec. Comment must reflect reality so future readers don't think cross-task rebinding is a real path.

- [ ] **Step 1: Edit the comment and tighten the condition**

Locate `_resume_batch` (line ~1242) in `agents/tg_transfer/__main__.py` and replace:

```python
    async def _resume_batch(self, task_id: str, job_id: str, job: dict):
        """Resume a paused batch job (non-blocking)."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])
        chat_id = self._current_chat_id.get(task_id, 0)

        # If the reply came in under a new task_id (e.g. hub created a fresh
        # task), rewrite the DB binding so future progress goes to the new task.
        if job.get("task_id") != task_id or job.get("chat_id") != chat_id:
            await self.db.update_job_binding(job_id, task_id, chat_id)
```

with:

```python
    async def _resume_batch(self, task_id: str, job_id: str, job: dict):
        """Resume a paused batch job (non-blocking)."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])
        chat_id = self._current_chat_id.get(task_id, 0)

        # task_id is invariant per the _pending_jobs guarantee (hub's reply
        # routes back under the same task_id we registered, hub never reuses
        # a task_id, and _pending_jobs is keyed by task_id). Only chat_id
        # may diverge if the user replied from a different chat.
        if job.get("chat_id") != chat_id:
            await self.db.update_job_binding(job_id, task_id, chat_id)
```

- [ ] **Step 2: Run the full test suite to confirm no regression**

Run: `pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add agents/tg_transfer/__main__.py
git commit -m "refactor(tg-transfer): tighten _resume_batch rebinding to chat_id only"
```

---

## Task 10: Manual end-to-end verification

This task has no test code — it confirms the change works on real infrastructure before declaring done. Per project memory rule "修 bug 後務必 commit + build + deploy 一次完成", this is the deployment gate.

- [ ] **Step 1: Build and start the stack**

Run: `docker-compose up -d --build tg_transfer hub`
Expected: both containers come up, no startup errors in `docker-compose logs --tail=100 tg_transfer hub`.

- [ ] **Step 2: Verify legacy migration ran**

Run: `docker-compose exec tg_transfer ls -la /data/tmp/ | head -20`
Expected: `.migrated_v2` flag file present; no stray `.mp4`/`.jpg` at root level (these would have been swept).

- [ ] **Step 3: Trigger a small batch transfer (5-10 messages)**

Via Telegram: send a `/batch` command to the agent, target a small album-heavy source.

While transfer runs:
```bash
docker-compose exec tg_transfer ls /data/tmp/
```
Expected: a single `{task_id}/` subdirectory containing in-flight downloads.

- [ ] **Step 4: Delete the conversation from hub dashboard**

Open hub dashboard, find the in-flight task, click delete.

Verify:
```bash
docker-compose exec tg_transfer ls /data/tmp/
docker-compose exec tg_transfer sqlite3 /data/transfer.db \
    "SELECT task_id, status FROM jobs;"
```
Expected: the `{task_id}/` directory gone within ~1 second; `jobs` table no longer contains that task.

- [ ] **Step 5: Test orphan-scan path (offline delete)**

```bash
docker-compose stop tg_transfer
# In hub dashboard, delete a different active task while agent is down.
docker-compose start tg_transfer
sleep 5
docker-compose exec tg_transfer ls /data/tmp/
```
Expected: the deleted task's directory is gone after agent restart even though it never received the WS message.

- [ ] **Step 6: Document outcome and close**

If all pass, the deployment is verified. If anything fails, capture the failing step + agent logs and roll back via `git revert <range>`; the migration flag means a partial revert needs care (drop `.migrated_v2` so re-deploy migrates again).

---

## Self-Review

Spec coverage check:

| Spec section | Implementing task |
|---|---|
| 1. Directory structure `tmp/{task_id}/` | Task 3 |
| 2. TransferEngine `task_id` parameter | Task 3 + Task 4 |
| 3a. Hub TASK_DELETED ws message | Task 1 (enum) + Task 5 (hub emit) |
| 3b. Agent on_task_deleted handler | Task 1 (base hook) + Task 6 (tg_transfer impl) |
| 3c. Agent startup orphan scan | Task 7 |
| 4. No proactive rmdir (rely on orphan scan) | Confirmed by absence — Task 3 does NOT add rmdir; orphan scan in Task 7 handles cleanup |
| 5. Remove misleading dead-branch comment | Task 9 |
| 6. Migration with `.migrated_v2` flag | Task 8 |
| Test plan from spec | Task 1 (unit), Task 3 (engine path test), Task 5 (hub-emit), Task 6 (delete cleanup), Task 7 (orphan), Task 8 (migration), Task 10 (manual e2e) |

No placeholders, type signatures consistent (`task_id: str = None` everywhere; `delete_jobs_by_task` returns `int`; `get_active_task_ids` returns `set[str]`; `clear_all_partials` returns `int`).

Plan is ready.
