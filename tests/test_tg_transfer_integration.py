"""Integration tests for TG Transfer Agent — tests the full flow without real Telegram."""
import os
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from core.models import TaskRequest, TaskStatus


@pytest_asyncio.fixture
async def db(tmp_path):
    from agents.tg_transfer.db import TransferDB
    database = TransferDB(str(tmp_path / "integration.db"))
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def mock_tg_client():
    client = AsyncMock()

    def make_msg(msg_id, text="test", media=None, grouped_id=None):
        m = MagicMock()
        m.id = msg_id
        m.text = text
        m.message = text
        m.media = media
        m.grouped_id = grouped_id
        m.photo = None
        m.video = None
        m.document = None
        m.sticker = None
        m.poll = None
        m.voice = None
        m.date = MagicMock()
        m.date.strftime = MagicMock(return_value="2026-04-17")
        return m

    client._make_msg = make_msg
    return client


@pytest.mark.asyncio
async def test_single_transfer_text_only_is_skipped(db, mock_tg_client):
    """Text-only source message (no media) is now skipped by policy — the
    transfer tool is media-only. Previously this test verified that we
    forwarded the text via send_message; that behaviour was removed after
    users reported chatty source chats flooding the target with text."""
    from agents.tg_transfer.transfer_engine import TransferEngine

    msg = mock_tg_client._make_msg(123, text="just text, no media")
    mock_tg_client.get_messages = AsyncMock(return_value=msg)
    mock_tg_client.get_entity = AsyncMock(return_value=MagicMock())
    mock_tg_client.send_message = AsyncMock()

    engine = TransferEngine(client=mock_tg_client, db=db, tmp_dir="/tmp/test_transfer")
    target = MagicMock()
    source = MagicMock()

    result = await engine.transfer_single(source, target, msg)
    assert result["ok"] is False
    mock_tg_client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_batch_with_dedup(db):
    """Dedup now reads from the media table (via media_db param). Without
    media_db the call returns an empty set — the safe no-skip fallback used
    in unit tests that don't wire up MediaDB. Cross-job dedup using the media
    table is covered in tests/test_db.py (test_get_transferred_message_ids_*)."""
    job1 = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job1, [1, 2, 3])
    await db.mark_message(job1, 1, "success")
    await db.mark_message(job1, 2, "success")
    await db.mark_message(job1, 3, "success")

    # Without media_db, the function returns empty set (safe default).
    already = await db.get_transferred_message_ids("@src", "@dst")
    assert already == set()


@pytest.mark.asyncio
async def test_config_persistence(db):
    """Config set via bot should persist and be retrievable."""
    await db.set_config("default_target_chat", "@first")
    assert await db.get_config("default_target_chat") == "@first"

    await db.set_config("default_target_chat", "@second")
    assert await db.get_config("default_target_chat") == "@second"


async def _build_test_agent(tmp_path):
    """Bare-minimum TGTransferAgent for unit tests: real DB + engine,
    fake TG client, no WS, no Hub."""
    from agents.tg_transfer.__main__ import TGTransferAgent
    from agents.tg_transfer.db import TransferDB
    from agents.tg_transfer.transfer_engine import TransferEngine

    agent = TGTransferAgent.__new__(TGTransferAgent)
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


@pytest.mark.asyncio
async def test_on_task_deleted_removes_dir_and_db_rows(tmp_path):
    """Calling on_task_deleted must rmtree tmp/{task_id}/ and delete the
    task's jobs + job_messages, plus drop in-memory state."""
    import shutil
    from agents.tg_transfer.__main__ import TGTransferAgent

    # Set up: real TransferDB with one bound job, and a tmp/{task_id}/ dir
    # containing a fake artefact.
    agent = await _build_test_agent(tmp_path)
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
