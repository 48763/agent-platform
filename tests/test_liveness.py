import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from agents.tg_transfer.liveness_checker import check_batch
from agents.tg_transfer.media_db import MediaDB


@pytest_asyncio.fixture
async def mdb(tmp_path):
    db = MediaDB(str(tmp_path / "liveness_test.db"))
    await db.init()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_check_batch_alive(mdb):
    """Messages that still exist should get last_checked_at updated."""
    m1 = await mdb.insert_media(
        sha256="live1", phash=None, file_type="photo", file_size=100,
        caption=None, source_chat="@s", source_msg_id=1,
        target_chat="@dst", job_id="j1",
    )
    await mdb.mark_uploaded(m1, target_msg_id=50)

    client = AsyncMock()
    msg = MagicMock()
    msg.id = 50
    client.get_messages = AsyncMock(return_value=msg)

    deleted, checked = await check_batch(client, mdb, [await mdb.get_media(m1)])
    assert checked == 1
    assert deleted == 0


@pytest.mark.asyncio
async def test_check_batch_dead(mdb):
    """Messages that no longer exist should be deleted from DB."""
    m1 = await mdb.insert_media(
        sha256="dead1", phash=None, file_type="photo", file_size=100,
        caption=None, source_chat="@s", source_msg_id=2,
        target_chat="@dst", job_id="j1",
    )
    await mdb.mark_uploaded(m1, target_msg_id=51)

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=None)

    deleted, checked = await check_batch(client, mdb, [await mdb.get_media(m1)])
    assert deleted == 1
    assert checked == 1
    assert await mdb.get_media(m1) is None
