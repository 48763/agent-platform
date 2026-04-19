import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import os
from agents.tg_transfer.transfer_engine import TransferEngine
from agents.tg_transfer.db import TransferDB


@pytest_asyncio.fixture
async def db(tmp_path):
    database = TransferDB(str(tmp_path / "test.db"))
    await database.init()
    yield database
    await database.close()


def _make_message(msg_id, text=None, media=True, grouped_id=None):
    msg = MagicMock()
    msg.id = msg_id
    msg.text = text
    msg.message = text
    msg.media = MagicMock() if media else None
    msg.grouped_id = grouped_id
    msg.photo = MagicMock() if media else None
    msg.video = None
    msg.document = None
    msg.sticker = None
    msg.poll = None
    msg.voice = None
    return msg


@pytest.fixture
def mock_client():
    client = AsyncMock()
    return client


@pytest.fixture
def engine(mock_client, db, tmp_path):
    return TransferEngine(
        client=mock_client,
        db=db,
        tmp_dir=str(tmp_path / "downloads"),
        retry_limit=2,
        progress_interval=2,
    )


@pytest.mark.asyncio
async def test_transfer_single_message(engine, mock_client, db):
    source_entity = MagicMock()
    target_entity = MagicMock()
    msg = _make_message(100, text="hello", media=False)
    mock_client.get_messages = AsyncMock(return_value=[msg])
    mock_client.send_message = AsyncMock()

    job_id = await db.create_job("@src", "@dst", "single")
    await db.add_messages(job_id, [100])
    await db.update_job_status(job_id, "running")

    result = await engine.transfer_single(source_entity, target_entity, msg)
    assert result["ok"] is True
    mock_client.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_should_skip_sticker(engine):
    msg = _make_message(101)
    msg.sticker = MagicMock()
    msg.photo = None
    msg.media = msg.sticker
    assert engine.should_skip(msg) is True


@pytest.mark.asyncio
async def test_should_skip_poll(engine):
    msg = _make_message(102)
    msg.poll = MagicMock()
    msg.photo = None
    msg.sticker = None
    msg.media = msg.poll
    assert engine.should_skip(msg) is True


@pytest.mark.asyncio
async def test_should_not_skip_photo(engine):
    msg = _make_message(103)
    msg.sticker = None
    msg.poll = None
    msg.voice = None
    assert engine.should_skip(msg) is False


def _make_video_message(msg_id, text=None):
    msg = MagicMock()
    msg.id = msg_id
    msg.text = text
    msg.message = text
    msg.media = MagicMock()
    msg.grouped_id = None
    msg.photo = None
    msg.video = MagicMock()
    msg.document = None
    msg.sticker = None
    msg.poll = None
    msg.voice = None
    return msg


@pytest.mark.asyncio
async def test_transfer_media_video_sends_attributes(engine, mock_client, tmp_path):
    """Video upload should include DocumentAttributeVideo with metadata."""
    target_entity = MagicMock()
    msg = _make_video_message(200, text="test video")

    video_path = str(tmp_path / "downloads" / "200" / "video.mp4")
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 100)
    mock_client.download_media = AsyncMock(return_value=video_path)

    sent = MagicMock()
    sent.id = 999
    mock_client.send_file = AsyncMock(return_value=sent)

    with patch("agents.tg_transfer.transfer_engine.ffprobe_metadata", new_callable=AsyncMock) as mock_ffprobe:
        mock_ffprobe.return_value = {"duration": 120, "width": 1920, "height": 1080}
        with patch("agents.tg_transfer.transfer_engine.compute_sha256", return_value="abc123"):
            with patch("agents.tg_transfer.transfer_engine.compute_phash_video", new_callable=AsyncMock, return_value=None):
                result = await engine.transfer_single(MagicMock(), target_entity, msg)

    assert result["ok"] is True
    call_kwargs = mock_client.send_file.call_args
    assert call_kwargs.kwargs.get("supports_streaming") is True
    attrs = call_kwargs.kwargs.get("attributes")
    assert attrs is not None
    assert len(attrs) == 1
    assert attrs[0].duration == 120
    assert attrs[0].w == 1920
    assert attrs[0].h == 1080


@pytest.mark.asyncio
async def test_transfer_album_atomic_download_failure(engine, mock_client, tmp_path):
    """If any media in album fails to download, entire album should fail."""
    target_entity = MagicMock()
    msg1 = _make_message(301, text="caption", grouped_id=10)
    msg2 = _make_message(302, grouped_id=10)

    download_results = [str(tmp_path / "file1.jpg"), None]
    mock_client.download_media = AsyncMock(side_effect=download_results)
    mock_client.send_file = AsyncMock()

    result = await engine.transfer_album(target_entity, [msg1, msg2])

    assert result is False
    mock_client.send_file.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_album_parallel_download(engine, mock_client, tmp_path):
    """Album downloads should succeed when all files download."""
    target_entity = MagicMock()
    msg1 = _make_message(401, text="caption", grouped_id=20)
    msg2 = _make_message(402, grouped_id=20)

    path1 = str(tmp_path / "downloads" / "album" / "file1.jpg")
    path2 = str(tmp_path / "downloads" / "album" / "file2.jpg")
    os.makedirs(os.path.dirname(path1), exist_ok=True)
    for p in [path1, path2]:
        with open(p, "wb") as f:
            f.write(b"\x00" * 10)

    mock_client.download_media = AsyncMock(side_effect=[path1, path2])
    mock_client.send_file = AsyncMock()

    result = await engine.transfer_album(target_entity, [msg1, msg2])

    assert result is True
    mock_client.send_file.assert_called_once()
