import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import os
from agents.tg_transfer.transfer_engine import TransferEngine
from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.media_db import MediaDB


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


@pytest_asyncio.fixture
async def media_db(tmp_path):
    mdb = MediaDB(str(tmp_path / "media.db"))
    await mdb.init()
    yield mdb
    await mdb.close()


@pytest.fixture
def engine_with_media_db(mock_client, db, media_db, tmp_path):
    return TransferEngine(
        client=mock_client,
        db=db,
        tmp_dir=str(tmp_path / "downloads"),
        retry_limit=2,
        progress_interval=2,
        media_db=media_db,
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
async def test_transfer_media_uses_flat_tmp_dir_no_subdir(engine, mock_client, tmp_path):
    """Downloading media should not create a per-message subdirectory under
    tmp_dir. Everything stays directly in tmp_dir so we avoid metadata churn
    (mkdir + rmtree on every transfer) that correlates with Mac APFS stalls."""
    target_entity = MagicMock()
    msg = _make_message(500, text="flat", media=True)
    msg.document = None  # no thumbs; keep the test focused on tmp_dir layout

    captured = {}

    async def fake_download_media(message, file=None, **kw):
        # Snapshot the tmp_dir tree at the moment download_media is called
        # (before Telethon would write anything).
        entries = []
        if os.path.isdir(engine.tmp_dir):
            for name in os.listdir(engine.tmp_dir):
                entries.append((name, os.path.isdir(os.path.join(engine.tmp_dir, name))))
        captured.setdefault("tmp_snapshots", []).append(entries)
        captured["file"] = file
        # Emulate Telethon: write to file arg (whether dir or file path).
        if file and os.path.isdir(file):
            out = os.path.join(file, "photo.jpg")
        else:
            out = file
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"\x00" * 10)
        return out

    mock_client.download_media = AsyncMock(side_effect=fake_download_media)
    sent = MagicMock()
    sent.id = 9999
    mock_client.send_file = AsyncMock(return_value=sent)

    with patch("agents.tg_transfer.transfer_engine.compute_sha256", return_value="flat"):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            await engine.transfer_single(MagicMock(), target_entity, msg)

    # No subdirectory should exist in tmp_dir during the download call.
    subdirs_during = [
        [name for name, is_dir in snap if is_dir]
        for snap in captured.get("tmp_snapshots", [])
    ]
    assert all(not sd for sd in subdirs_during), (
        f"Engine created subdirectories during download: {subdirs_during}"
    )


@pytest.mark.asyncio
async def test_transfer_media_video_passes_tg_thumb(engine, mock_client, tmp_path):
    """When message has a thumbnail (msg.document.thumbs), download it and pass
    as thumb= to send_file so TG shows preview cover."""
    target_entity = MagicMock()
    msg = _make_video_message(220, text="with thumb")
    msg.document = MagicMock()
    thumb_obj = MagicMock()
    msg.document.thumbs = [MagicMock(), thumb_obj]  # last = largest

    video_path = str(tmp_path / "downloads" / "220" / "video.mp4")
    thumb_path = str(tmp_path / "downloads" / "220" / "thumb.jpg")
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    for p in [video_path, thumb_path]:
        with open(p, "wb") as f:
            f.write(b"\x00" * 10)

    # download_media called twice: main media, then thumb
    mock_client.download_media = AsyncMock(side_effect=[video_path, thumb_path])

    sent = MagicMock()
    sent.id = 1001
    mock_client.send_file = AsyncMock(return_value=sent)

    with patch("agents.tg_transfer.transfer_engine.ffprobe_metadata", new_callable=AsyncMock) as mock_ffprobe:
        mock_ffprobe.return_value = {"duration": 10, "width": 1280, "height": 720}
        with patch("agents.tg_transfer.transfer_engine.compute_sha256", return_value="thumb_test"):
            with patch("agents.tg_transfer.transfer_engine.compute_phash_video", new_callable=AsyncMock, return_value=None):
                result = await engine.transfer_single(MagicMock(), target_entity, msg)

    assert result["ok"] is True
    call_kwargs = mock_client.send_file.call_args
    assert call_kwargs.kwargs.get("thumb") == thumb_path


@pytest.mark.asyncio
async def test_transfer_media_video_no_thumb_when_message_has_none(
    engine, mock_client, tmp_path
):
    """No thumbs on message → don't pass thumb= (and don't fail)."""
    target_entity = MagicMock()
    msg = _make_video_message(221, text="no thumb")
    msg.document = MagicMock()
    msg.document.thumbs = None

    video_path = str(tmp_path / "downloads" / "221" / "video.mp4")
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 10)
    mock_client.download_media = AsyncMock(return_value=video_path)

    sent = MagicMock()
    sent.id = 1002
    mock_client.send_file = AsyncMock(return_value=sent)

    with patch("agents.tg_transfer.transfer_engine.ffprobe_metadata", new_callable=AsyncMock) as mock_ffprobe:
        mock_ffprobe.return_value = {"duration": 5, "width": 640, "height": 480}
        with patch("agents.tg_transfer.transfer_engine.compute_sha256", return_value="no_thumb"):
            with patch("agents.tg_transfer.transfer_engine.compute_phash_video", new_callable=AsyncMock, return_value=None):
                result = await engine.transfer_single(MagicMock(), target_entity, msg)

    assert result["ok"] is True
    call_kwargs = mock_client.send_file.call_args
    assert "thumb" not in call_kwargs.kwargs or call_kwargs.kwargs.get("thumb") is None


@pytest.mark.asyncio
async def test_transfer_media_video_falls_back_to_msg_file_when_ffprobe_fails(
    engine, mock_client, tmp_path
):
    """When ffprobe returns None, use msg.file.width/height/duration instead
    of sending no attributes (which causes TG to show wrong aspect ratio)."""
    target_entity = MagicMock()
    msg = _make_video_message(210, text="fallback video")
    msg.file = MagicMock()
    msg.file.width = 720
    msg.file.height = 1280
    msg.file.duration = 45

    video_path = str(tmp_path / "downloads" / "210" / "video.mp4")
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 10)
    mock_client.download_media = AsyncMock(return_value=video_path)

    sent = MagicMock()
    sent.id = 1000
    mock_client.send_file = AsyncMock(return_value=sent)

    with patch("agents.tg_transfer.transfer_engine.ffprobe_metadata", new_callable=AsyncMock) as mock_ffprobe:
        mock_ffprobe.return_value = None  # ffprobe fails
        with patch("agents.tg_transfer.transfer_engine.compute_sha256", return_value="xyz"):
            with patch("agents.tg_transfer.transfer_engine.compute_phash_video", new_callable=AsyncMock, return_value=None):
                result = await engine.transfer_single(MagicMock(), target_entity, msg)

    assert result["ok"] is True
    call_kwargs = mock_client.send_file.call_args
    attrs = call_kwargs.kwargs.get("attributes")
    assert attrs is not None, "Expected DocumentAttributeVideo from msg.file fallback"
    assert attrs[0].duration == 45
    assert attrs[0].w == 720
    assert attrs[0].h == 1280


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
async def test_transfer_album_writes_media_db_per_file(
    engine_with_media_db, mock_client, media_db, tmp_path
):
    """Regression: transfer_album must insert one media row per file and mark
    each as uploaded, so dashboard stats match actual transfer count. Previously
    albums bypassed media_db entirely, causing total_media to under-count."""
    target_entity = MagicMock()
    msg1 = _make_message(501, text="album caption", grouped_id=50)
    msg2 = _make_message(502, grouped_id=50)
    msg3 = _make_message(503, grouped_id=50)

    paths = []
    for i, msg_id in enumerate([501, 502, 503]):
        p = str(tmp_path / "downloads" / f"f{msg_id}.jpg")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(bytes([i]) * 10)  # different content → different sha256
        paths.append(p)

    mock_client.download_media = AsyncMock(side_effect=paths)

    sent_msgs = []
    for i, msg_id in enumerate([501, 502, 503]):
        sm = MagicMock()
        sm.id = 9000 + i
        sent_msgs.append(sm)
    mock_client.send_file = AsyncMock(return_value=sent_msgs)

    with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
        ok = await engine_with_media_db.transfer_album(
            target_entity, [msg1, msg2, msg3],
            target_chat="@dst", source_chat="@src", job_id="job-album",
        )

    assert ok is True
    stats = await media_db.get_stats()
    assert stats["total_media"] == 3, (
        f"expected 3 uploaded media rows, got {stats['total_media']}"
    )


@pytest.mark.asyncio
async def test_transfer_album_download_fail_no_media_row(
    engine_with_media_db, mock_client, media_db, tmp_path
):
    """Download failure in album → no media rows inserted (we only insert
    after all downloads succeed)."""
    target_entity = MagicMock()
    msg1 = _make_message(601, text="cap", grouped_id=60)
    msg2 = _make_message(602, grouped_id=60)

    mock_client.download_media = AsyncMock(
        side_effect=[str(tmp_path / "f1.jpg"), None]
    )
    mock_client.send_file = AsyncMock()

    # First download path must exist for the test setup consistency
    p = str(tmp_path / "f1.jpg")
    with open(p, "wb") as f:
        f.write(b"\x00" * 5)

    ok = await engine_with_media_db.transfer_album(
        target_entity, [msg1, msg2],
        target_chat="@dst", source_chat="@src", job_id="job-fail",
    )
    assert ok is False
    stats = await media_db.get_stats()
    assert stats["total_media"] == 0


@pytest.mark.asyncio
async def test_transfer_album_upload_exception_marks_failed_not_deletes(
    engine_with_media_db, mock_client, media_db, tmp_path
):
    """If send_file raises, media rows are kept but marked 'failed' so next
    job can retry. Not deleted — that would lose state across restarts."""
    target_entity = MagicMock()
    msg1 = _make_message(701, text="cap", grouped_id=70)
    msg2 = _make_message(702, grouped_id=70)

    paths = []
    for mid in [701, 702]:
        p = str(tmp_path / "downloads" / f"f{mid}.jpg")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(bytes([mid % 256]) * 10)
        paths.append(p)

    mock_client.download_media = AsyncMock(side_effect=paths)
    mock_client.send_file = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
        with pytest.raises(RuntimeError):
            await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2],
                target_chat="@dst", source_chat="@src", job_id="job-rollback",
            )

    stats = await media_db.get_stats()
    # uploaded count stays 0
    assert stats["total_media"] == 0
    # But the rows persist as 'failed' for retry
    assert stats["by_status"]["failed"] == 2


@pytest.mark.asyncio
async def test_transfer_single_upload_exception_marks_failed_not_deletes(
    engine_with_media_db, mock_client, media_db, tmp_path
):
    """Single-media path: same policy — exception marks failed, not delete."""
    target_entity = MagicMock()
    msg = _make_message(800, text="single", media=True)
    msg.document = None

    video_path = str(tmp_path / "downloads" / "vid.dat")
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 10)

    mock_client.download_media = AsyncMock(return_value=video_path)
    mock_client.send_file = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("agents.tg_transfer.transfer_engine.compute_sha256", return_value="singlesha"):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            with pytest.raises(RuntimeError):
                await engine_with_media_db._transfer_media(
                    target_entity, msg,
                    target_chat="@d", source_chat="@s", job_id="jsingle",
                )

    stats = await media_db.get_stats()
    assert stats["total_media"] == 0
    assert stats["by_status"]["failed"] == 1


@pytest.mark.asyncio
async def test_transfer_album_skips_duplicate_file_sends_rest(
    engine_with_media_db, mock_client, media_db, tmp_path
):
    """Option B: if one file in album has sha256 already uploaded to target,
    remove that file and send the rest of the album."""
    # Pre-populate: sha 'dup_sha' already uploaded to @dst
    existing = await media_db.insert_media(
        sha256="dup_sha", phash=None, file_type="photo",
        file_size=10, caption="prior", source_chat="@old",
        source_msg_id=1, target_chat="@dst", job_id="old_job",
    )
    await media_db.mark_uploaded(existing, target_msg_id=77)

    target_entity = MagicMock()
    msg1 = _make_message(901, text="cap", grouped_id=90)
    msg2 = _make_message(902, grouped_id=90)
    msg3 = _make_message(903, grouped_id=90)

    p1 = str(tmp_path / "downloads" / "a.jpg")
    p2 = str(tmp_path / "downloads" / "b.jpg")
    p3 = str(tmp_path / "downloads" / "c.jpg")
    os.makedirs(os.path.dirname(p1), exist_ok=True)
    for p, data in [(p1, b"\x01" * 10), (p2, b"\x02" * 10), (p3, b"\x03" * 10)]:
        with open(p, "wb") as f:
            f.write(data)
    mock_client.download_media = AsyncMock(side_effect=[p1, p2, p3])

    # send_file returns 2 sent msgs (for 2 non-dup files)
    sent_msgs = [MagicMock(id=801), MagicMock(id=802)]
    mock_client.send_file = AsyncMock(return_value=sent_msgs)

    # Make msg2's file match the pre-uploaded sha
    sha_map = {p1: "sha_a", p2: "dup_sha", p3: "sha_c"}

    def fake_sha(path):
        return sha_map[path]

    with patch("agents.tg_transfer.transfer_engine.compute_sha256", side_effect=fake_sha):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2, msg3],
                target_chat="@dst", source_chat="@src", job_id="album_dup",
            )

    assert ok is True
    # send_file was called with only 2 paths (msg2 removed)
    call_args = mock_client.send_file.call_args
    sent_paths = call_args.args[1]
    assert len(sent_paths) == 2
    assert p2 not in sent_paths

    # media_db: 1 prior uploaded + 2 newly uploaded = 3 uploaded
    stats = await media_db.get_stats()
    assert stats["by_status"]["uploaded"] == 3


@pytest.mark.asyncio
async def test_transfer_album_all_duplicate_no_send_file(
    engine_with_media_db, mock_client, media_db, tmp_path
):
    """If every file in an album is a duplicate, send_file must NOT be called
    at all (no empty album upload)."""
    for sha in ["da", "db"]:
        mid = await media_db.insert_media(
            sha256=sha, phash=None, file_type="photo",
            file_size=10, caption=None, source_chat="@old",
            source_msg_id=0, target_chat="@dst", job_id="old",
        )
        await media_db.mark_uploaded(mid, target_msg_id=1)

    target_entity = MagicMock()
    msg1 = _make_message(1001, text="cap", grouped_id=100)
    msg2 = _make_message(1002, grouped_id=100)

    p1 = str(tmp_path / "downloads" / "x.jpg")
    p2 = str(tmp_path / "downloads" / "y.jpg")
    os.makedirs(os.path.dirname(p1), exist_ok=True)
    for p, d in [(p1, b"\x01" * 10), (p2, b"\x02" * 10)]:
        with open(p, "wb") as f:
            f.write(d)
    mock_client.download_media = AsyncMock(side_effect=[p1, p2])
    mock_client.send_file = AsyncMock()

    sha_map = {p1: "da", p2: "db"}

    with patch("agents.tg_transfer.transfer_engine.compute_sha256",
               side_effect=lambda p: sha_map[p]):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2],
                target_chat="@dst", source_chat="@src", job_id="all_dup",
            )

    assert ok is True
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
