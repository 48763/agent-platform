import pytest
import pytest_asyncio
from agents.tg_transfer.db import TransferDB


@pytest_asyncio.fixture
async def db(tmp_path):
    database = TransferDB(str(tmp_path / "test.db"))
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_create_job(db):
    job_id = await db.create_job(
        source_chat="@source",
        target_chat="@target",
        mode="batch",
        filter_type="count",
        filter_value='{"count": 50}',
    )
    job = await db.get_job(job_id)
    assert job["source_chat"] == "@source"
    assert job["target_chat"] == "@target"
    assert job["status"] == "pending"
    assert job["mode"] == "batch"


@pytest.mark.asyncio
async def test_add_messages_and_get_next_pending(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [100, 101, 102])
    msg = await db.get_next_pending(job_id)
    assert msg["message_id"] == 100


@pytest.mark.asyncio
async def test_mark_success(db):
    job_id = await db.create_job("@src", "@dst", "single")
    await db.add_messages(job_id, [200])
    await db.mark_message(job_id, 200, "success")
    msg = await db.get_next_pending(job_id)
    assert msg is None


@pytest.mark.asyncio
async def test_mark_failed_with_error(db):
    job_id = await db.create_job("@src", "@dst", "single")
    await db.add_messages(job_id, [300])
    await db.mark_message(job_id, 300, "failed", error="timeout")
    await db.increment_retry(job_id, 300)
    msg = await db.get_message(job_id, 300)
    assert msg["status"] == "failed"
    assert msg["retry_count"] == 1
    assert msg["error"] == "timeout"


@pytest.mark.asyncio
async def test_job_progress(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [1, 2, 3, 4, 5])
    await db.mark_message(job_id, 1, "success")
    await db.mark_message(job_id, 2, "success")
    await db.mark_message(job_id, 3, "skipped")
    progress = await db.get_progress(job_id)
    assert progress == {"total": 5, "success": 2, "failed": 0, "skipped": 1, "pending": 2}


@pytest.mark.asyncio
async def test_update_job_status(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.update_job_status(job_id, "running")
    job = await db.get_job(job_id)
    assert job["status"] == "running"


@pytest.mark.asyncio
async def test_set_auto_skip(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.set_auto_skip(job_id, True)
    job = await db.get_job(job_id)
    assert job["auto_skip"] == 1


@pytest.mark.asyncio
async def test_dedup_returns_success_ids_while_job_running(db):
    """Cross-job text-message dedup is sourced from job_messages.status.
    It works WHILE the job is alive (running / paused). Once the job
    reaches a terminal status the messages are pruned — see
    TestJobTerminalCleanup for the contract."""
    job1 = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job1, [10, 11, 12])
    await db.mark_message(job1, 10, "success")
    await db.mark_message(job1, 11, "success")
    await db.mark_message(job1, 12, "failed")
    already_done = await db.get_transferred_message_ids("@src", "@dst")
    assert already_done == {10, 11}


@pytest.mark.asyncio
async def test_dedup_empty_after_job_completed(db):
    """After terminal cleanup, job_messages are gone, so cross-job text
    dedup returns empty. Media dedup is unaffected (uses the media table)."""
    job1 = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job1, [10, 11])
    await db.mark_message(job1, 10, "success")
    await db.mark_message(job1, 11, "success")
    await db.update_job_status(job1, "completed")
    assert await db.get_transferred_message_ids("@src", "@dst") == set()


@pytest.mark.asyncio
async def test_config_get_set(db):
    await db.set_config("default_target_chat", "@my_backup")
    val = await db.get_config("default_target_chat")
    assert val == "@my_backup"
    await db.set_config("default_target_chat", "@new_backup")
    val = await db.get_config("default_target_chat")
    assert val == "@new_backup"


@pytest.mark.asyncio
async def test_config_get_missing(db):
    val = await db.get_config("nonexistent")
    assert val is None


@pytest.mark.asyncio
async def test_get_running_jobs(db):
    job1 = await db.create_job("@a", "@b", "batch")
    job2 = await db.create_job("@c", "@d", "batch")
    await db.update_job_status(job1, "running")
    await db.update_job_status(job2, "completed")
    running = await db.get_running_jobs()
    assert len(running) == 1
    assert running[0]["job_id"] == job1


@pytest.mark.asyncio
async def test_add_messages_with_grouped_id(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [50, 51, 52], grouped_ids={50: 999, 51: 999})
    msgs = await db.get_grouped_messages(job_id, 999)
    assert len(msgs) == 2
    assert {m["message_id"] for m in msgs} == {50, 51}


@pytest.mark.asyncio
async def test_reset_message_to_pending(db):
    job_id = await db.create_job("@src", "@dst", "single")
    await db.add_messages(job_id, [400])
    await db.mark_message(job_id, 400, "failed", error="network")
    await db.reset_message(job_id, 400)
    msg = await db.get_message(job_id, 400)
    assert msg["status"] == "pending"
    assert msg["error"] is None


@pytest.mark.asyncio
async def test_create_job_persists_task_id_and_chat_id(db):
    """Jobs must persist the TG task_id/chat_id so that agent can re-attach
    them to WS progress/result messages after an unannounced restart."""
    job_id = await db.create_job(
        source_chat="@src", target_chat="@dst", mode="batch",
        task_id="task-abc", chat_id=12345,
    )
    job = await db.get_job(job_id)
    assert job["task_id"] == "task-abc"
    assert job["chat_id"] == 12345


@pytest.mark.asyncio
async def test_get_resumable_jobs_includes_running_and_paused(db):
    """On startup the agent must pick up both running and paused jobs:
    running = was in the middle of transferring; paused = was waiting for a
    user decision (retry/skip). Both should be re-attached."""
    running = await db.create_job("@s", "@d", "batch", task_id="t1", chat_id=1)
    paused = await db.create_job("@s", "@d", "batch", task_id="t2", chat_id=2)
    done = await db.create_job("@s", "@d", "batch", task_id="t3", chat_id=3)
    pending = await db.create_job("@s", "@d", "batch", task_id="t4", chat_id=4)

    await db.update_job_status(running, "running")
    await db.update_job_status(paused, "paused")
    await db.update_job_status(done, "completed")
    # leave `pending` alone — it never started

    rows = await db.get_resumable_jobs()
    ids = {r["job_id"] for r in rows}
    assert running in ids
    assert paused in ids
    assert done not in ids
    assert pending not in ids


@pytest.mark.asyncio
async def test_update_job_binding_updates_task_id_and_chat_id(db):
    """When a paused job resumes under a new user reply, its task_id/chat_id
    may change — agent must be able to rewrite the binding so future progress
    goes to the new task."""
    job_id = await db.create_job(
        "@s", "@d", "batch", task_id="old", chat_id=100,
    )
    await db.update_job_binding(job_id, task_id="new", chat_id=200)
    job = await db.get_job(job_id)
    assert job["task_id"] == "new"
    assert job["chat_id"] == 200


@pytest.mark.asyncio
async def test_set_partial_persists_path_and_bytes(db):
    """Partial-download state must survive restart so we can resume from
    downloaded_bytes instead of starting over."""
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [500])
    await db.set_partial(job_id, 500, "/tmp/tg/500.dat", 64 * 1024 * 1024)
    msg = await db.get_message(job_id, 500)
    assert msg["partial_path"] == "/tmp/tg/500.dat"
    assert msg["downloaded_bytes"] == 64 * 1024 * 1024


@pytest.mark.asyncio
async def test_set_partial_overwrites_on_repeat_calls(db):
    """Each 64MB flush updates downloaded_bytes — latest call wins."""
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [501])
    await db.set_partial(job_id, 501, "/tmp/tg/501.dat", 64 * 1024 * 1024)
    await db.set_partial(job_id, 501, "/tmp/tg/501.dat", 128 * 1024 * 1024)
    msg = await db.get_message(job_id, 501)
    assert msg["downloaded_bytes"] == 128 * 1024 * 1024


@pytest.mark.asyncio
async def test_clear_partial_resets_state(db):
    """On successful upload, partial state is cleared so future resumes start
    fresh and cleanup can safely remove the artefact."""
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [502])
    await db.set_partial(job_id, 502, "/tmp/tg/502.dat", 999)
    await db.clear_partial(job_id, 502)
    msg = await db.get_message(job_id, 502)
    assert msg["partial_path"] is None
    assert msg["downloaded_bytes"] == 0


@pytest.mark.asyncio
async def test_migration_adds_partial_columns_to_legacy_job_messages(tmp_path):
    """Legacy DBs (before 1b) get partial_path/downloaded_bytes added without
    losing existing rows."""
    import aiosqlite
    path = str(tmp_path / "legacy_jm.db")
    legacy = await aiosqlite.connect(path)
    await legacy.executescript("""
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY, source_chat TEXT NOT NULL,
            target_chat TEXT NOT NULL, mode TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
        );
        CREATE TABLE job_messages (
            job_id      TEXT NOT NULL,
            message_id  INTEGER NOT NULL,
            grouped_id  INTEGER,
            status      TEXT DEFAULT 'pending',
            retry_count INTEGER DEFAULT 0,
            error       TEXT,
            PRIMARY KEY (job_id, message_id)
        );
        INSERT INTO jobs (job_id, source_chat, target_chat, mode, status)
            VALUES ('legacy_j', '@s', '@d', 'batch', 'running');
        INSERT INTO job_messages (job_id, message_id, status)
            VALUES ('legacy_j', 7, 'pending');
    """)
    await legacy.commit()
    await legacy.close()

    db = TransferDB(path)
    await db.init()
    try:
        m = await db.get_message("legacy_j", 7)
        assert m is not None
        assert m["partial_path"] is None
        assert m["downloaded_bytes"] == 0
        await db.set_partial("legacy_j", 7, "/tmp/x.dat", 1024)
        m = await db.get_message("legacy_j", 7)
        assert m["partial_path"] == "/tmp/x.dat"
        assert m["downloaded_bytes"] == 1024
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_adds_task_id_chat_id_to_legacy_jobs(tmp_path):
    """A DB created before this feature must be auto-migrated without losing
    data. Simulate a legacy DB by creating the old schema, then reopen via
    TransferDB.init() and confirm new columns exist + old rows are intact."""
    import aiosqlite
    path = str(tmp_path / "legacy.db")
    legacy = await aiosqlite.connect(path)
    await legacy.executescript("""
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            source_chat TEXT NOT NULL,
            target_chat TEXT NOT NULL,
            filter_type TEXT,
            filter_value TEXT,
            mode TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            auto_skip BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO jobs (job_id, source_chat, target_chat, mode, status)
        VALUES ('legacy_job', '@s', '@d', 'batch', 'running');
    """)
    await legacy.commit()
    await legacy.close()

    db = TransferDB(path)
    await db.init()
    try:
        job = await db.get_job("legacy_job")
        assert job is not None
        assert job["source_chat"] == "@s"
        # New columns exist, default NULL on migrated rows
        assert job["task_id"] is None
        assert job["chat_id"] is None
        # And new writes work
        await db.update_job_binding("legacy_job", task_id="t", chat_id=9)
        job = await db.get_job("legacy_job")
        assert job["task_id"] == "t"
        assert job["chat_id"] == 9
    finally:
        await db.close()


class TestJobTerminalCleanup:
    """When a job transitions to a terminal status (completed / failed /
    cancelled), its job_messages rows should be deleted to reclaim space. The
    jobs row itself stays so history is still visible."""

    @pytest.mark.asyncio
    async def test_completed_removes_job_messages(self, db):
        job_id = await db.create_job(
            source_chat="@s", target_chat="@t", mode="batch",
        )
        await db.add_messages(job_id, [1, 2, 3])
        await db.update_job_status(job_id, "completed")

        # Messages wiped, job row remains.
        assert await db.get_next_pending(job_id) is None
        assert await db.get_job(job_id) is not None

    @pytest.mark.asyncio
    async def test_failed_removes_job_messages(self, db):
        job_id = await db.create_job(
            source_chat="@s", target_chat="@t", mode="batch",
        )
        await db.add_messages(job_id, [10, 20])
        await db.update_job_status(job_id, "failed")

        assert await db.get_next_pending(job_id) is None
        assert await db.get_job(job_id) is not None

    @pytest.mark.asyncio
    async def test_cancelled_removes_job_messages(self, db):
        job_id = await db.create_job(
            source_chat="@s", target_chat="@t", mode="batch",
        )
        await db.add_messages(job_id, [5])
        await db.update_job_status(job_id, "cancelled")

        assert await db.get_next_pending(job_id) is None

    @pytest.mark.asyncio
    async def test_terminal_status_snapshots_progress(self, db):
        """Regression: a terminal transition deletes job_messages, but
        get_progress must still return the real final counts so the user
        sees 3/1/1 — not 0/0/0. Fix: snapshot progress before prune, return
        snapshot on reads once the job is terminal."""
        job_id = await db.create_job(
            source_chat="@s", target_chat="@t", mode="batch",
        )
        await db.add_messages(job_id, [1, 2, 3, 4, 5])
        await db.mark_message(job_id, 1, "success")
        await db.mark_message(job_id, 2, "success")
        await db.mark_message(job_id, 3, "success")
        await db.mark_message(job_id, 4, "skipped")
        await db.mark_message(job_id, 5, "failed")

        # Pre-transition — counts come from job_messages as before.
        progress = await db.get_progress(job_id)
        assert progress == {
            "total": 5, "success": 3, "failed": 1,
            "skipped": 1, "pending": 0,
        }

        await db.update_job_status(job_id, "completed")

        # Post-transition — job_messages is gone, but the snapshot must
        # preserve what the user actually sees in the "完成" message.
        progress = await db.get_progress(job_id)
        assert progress == {
            "total": 5, "success": 3, "failed": 1,
            "skipped": 1, "pending": 0,
        }

    @pytest.mark.asyncio
    async def test_non_terminal_preserves_job_messages(self, db):
        """Transitions like running / paused must NOT wipe messages."""
        job_id = await db.create_job(
            source_chat="@s", target_chat="@t", mode="batch",
        )
        await db.add_messages(job_id, [1, 2, 3])
        await db.update_job_status(job_id, "running")
        assert (await db.get_progress(job_id))["total"] == 3
        await db.update_job_status(job_id, "paused")
        assert (await db.get_progress(job_id))["total"] == 3
