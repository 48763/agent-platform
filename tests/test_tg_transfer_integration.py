"""Integration tests for TG Transfer Agent — tests the full flow without real Telegram."""
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
    """Cross-job text dedup via job_messages works WHILE the job is still
    alive. Once the job reaches a terminal status, job_messages are pruned
    to keep DB small, so dedup would return empty — media dedup (via the
    media table) is the long-term guard there."""
    job1 = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job1, [1, 2, 3])
    await db.mark_message(job1, 1, "success")
    await db.mark_message(job1, 2, "success")
    await db.mark_message(job1, 3, "success")
    # Do NOT mark completed here — that would wipe the per-message rows.

    already = await db.get_transferred_message_ids("@src", "@dst")
    assert already == {1, 2, 3}

    new_msg_ids = [1, 2, 3, 4, 5]
    to_add = [mid for mid in new_msg_ids if mid not in already]
    assert to_add == [4, 5]


@pytest.mark.asyncio
async def test_config_persistence(db):
    """Config set via bot should persist and be retrievable."""
    await db.set_config("default_target_chat", "@first")
    assert await db.get_config("default_target_chat") == "@first"

    await db.set_config("default_target_chat", "@second")
    assert await db.get_config("default_target_chat") == "@second"
