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
async def test_dedup_returns_existing_success_ids(db):
    job1 = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job1, [10, 11, 12])
    await db.mark_message(job1, 10, "success")
    await db.mark_message(job1, 11, "success")
    await db.mark_message(job1, 12, "failed")
    await db.update_job_status(job1, "completed")
    already_done = await db.get_transferred_message_ids("@src", "@dst")
    assert already_done == {10, 11}


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
