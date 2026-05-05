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
