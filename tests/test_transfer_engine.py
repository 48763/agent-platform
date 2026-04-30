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


def _stub_album_upload(mock_client, upload_side_effect=None):
    """Configure mock_client so `transfer_album`'s manual upload path
    (upload_file + UploadMediaRequest + SendMultiMediaRequest) completes
    without exceptions in tests that don't care about the wire format.

    Returns `req_log` — a list of all TL requests the engine sent to
    `client(...)`, in order — so tests can still assert call counts etc.

    `upload_side_effect`: optional callable invoked for each `upload_file`
    call (e.g. to raise, to record paths). Default is no-op.
    """
    from telethon.tl.types import Document, Photo, PhotoSize
    from telethon.tl.functions.messages import (
        UploadMediaRequest, SendMultiMediaRequest,
    )

    async def _upload(path, **kwargs):
        if upload_side_effect is not None:
            return upload_side_effect(path, **kwargs)
        return MagicMock(name="file_handle")
    mock_client.upload_file = AsyncMock(side_effect=_upload)

    req_log = []
    counter = {"n": 0}

    async def _call(request):
        req_log.append(request)
        counter["n"] += 1
        if isinstance(request, UploadMediaRequest):
            # Return both `photo` and `document` fields populated with real TL
            # objects so `tl_utils.get_input_media` works for either branch —
            # the engine picks one based on the file_type it detected.
            r = MagicMock()
            r.document = Document(
                id=7_000_000 + counter["n"],
                access_hash=1,
                file_reference=b"\x00",
                date=None,
                mime_type="video/mp4",
                size=0,
                dc_id=2,
                attributes=[],
                thumbs=[],
            )
            r.photo = Photo(
                id=8_000_000 + counter["n"],
                access_hash=2,
                file_reference=b"\x00",
                date=None,
                sizes=[PhotoSize(type="m", w=320, h=320, size=1)],
                dc_id=2,
            )
            return r
        if isinstance(request, SendMultiMediaRequest):
            upd = MagicMock()
            upd.updates = []
            return upd
        return MagicMock()
    mock_client.side_effect = _call

    # Telethon's internal helper that maps the Updates result back to a
    # Message list — stub it so media_db.mark_uploaded sees real ints.
    def _stub_response(random_ids, result, entity):
        return [MagicMock(id=5000 + i) for i in range(len(random_ids))]
    mock_client._get_response_message = _stub_response

    return req_log


@pytest.mark.asyncio
async def test_transfer_single_text_only_is_skipped(engine, mock_client, db):
    """Policy: pure text messages are never transferred — this tool is for
    media migration. Calling transfer_single on a text-only message returns
    a skip result (ok=False, no media effects) and does NOT hit send_message.

    Previously we forwarded bare text, which made chatty source chats replay
    their entire conversation into the target; users asked for it to stop."""
    source_entity = MagicMock()
    target_entity = MagicMock()
    msg = _make_message(100, text="hello", media=False)
    mock_client.get_messages = AsyncMock(return_value=[msg])
    mock_client.send_message = AsyncMock()

    job_id = await db.create_job("@src", "@dst", "single")
    await db.add_messages(job_id, [100])
    await db.update_job_status(job_id, "running")

    result = await engine.transfer_single(source_entity, target_entity, msg)
    assert result["ok"] is False
    assert result["dedup"] is False
    mock_client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_should_skip_text_only(engine):
    """Text-only messages (no media attached) must be marked skip so
    run_batch records them as 'skipped' instead of forwarding."""
    msg = _make_message(150, text="just typing", media=False)
    assert engine.should_skip(msg) is True


@pytest.mark.asyncio
async def test_should_not_skip_photo_with_caption(engine):
    """Caption text on a media message is NOT text-only — the media still
    travels (with caption preserved by _transfer_media)."""
    msg = _make_message(151, text="nice photo", media=True)
    assert engine.should_skip(msg) is False


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
async def test_transfer_media_video_no_thumb_when_all_sources_fail(
    engine, mock_client, tmp_path
):
    """No TG thumb on message AND ffmpeg extraction also fails → don't pass
    thumb= (and don't fail the transfer). Videos always attempt the local
    extraction fallback first; only when both sources come back empty does
    the upload proceed without a thumb."""
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
                with patch(
                    "agents.tg_transfer.transfer_engine.extract_video_thumb",
                    new_callable=AsyncMock, return_value=None,
                ):
                    result = await engine.transfer_single(MagicMock(), target_entity, msg)

    assert result["ok"] is True
    call_kwargs = mock_client.send_file.call_args
    assert "thumb" not in call_kwargs.kwargs or call_kwargs.kwargs.get("thumb") is None


@pytest.mark.asyncio
async def test_transfer_media_video_extracts_local_thumb_when_tg_has_none(
    engine, mock_client, tmp_path
):
    """Source is a `send as file` video without a TG-attached thumb →
    fall back to ffmpeg-extracted JPEG frame so the feed preview still
    renders instead of a blank document tile.

    Regression for: 影片沒預覽 + 0:00 + 大小硬塞在視窗. The three symptoms
    show together when a video uploads without DocumentAttributeVideo AND
    without a thumb — fixing the thumb path is one half of the repair
    (ffprobe_metadata now also hardened; see media_utils tests)."""
    target_entity = MagicMock()
    msg = _make_video_message(222, text="no TG thumb but local ok")
    msg.document = MagicMock()
    msg.document.thumbs = None  # TG-side has nothing

    video_path = str(tmp_path / "downloads" / "222" / "video.mp4")
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 10)
    mock_client.download_media = AsyncMock(return_value=video_path)

    sent = MagicMock()
    sent.id = 1003
    mock_client.send_file = AsyncMock(return_value=sent)

    # Simulate ffmpeg writing a real thumb file to whatever path the
    # engine picked — copy the arg into a returned path so assertions
    # downstream can compare.
    async def fake_extract(src, dest, **kwargs):
        with open(dest, "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")  # tiny valid JPEG-ish
        return dest

    with patch("agents.tg_transfer.transfer_engine.ffprobe_metadata", new_callable=AsyncMock) as mock_ffprobe:
        mock_ffprobe.return_value = {"duration": 5, "width": 640, "height": 480}
        with patch("agents.tg_transfer.transfer_engine.compute_sha256", return_value="local_thumb"):
            with patch("agents.tg_transfer.transfer_engine.compute_phash_video", new_callable=AsyncMock, return_value=None):
                with patch(
                    "agents.tg_transfer.transfer_engine.extract_video_thumb",
                    new_callable=AsyncMock, side_effect=fake_extract,
                ) as mock_extract:
                    result = await engine.transfer_single(MagicMock(), target_entity, msg)

    assert result["ok"] is True
    mock_extract.assert_awaited_once()
    call_kwargs = mock_client.send_file.call_args
    thumb_arg = call_kwargs.kwargs.get("thumb")
    assert thumb_arg, "Expected local thumb path to be forwarded to send_file"
    assert thumb_arg.endswith(".thumb.jpg")


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

    # Stream per-file chunks via iter_download (new resumable path)
    def _make_iter(msg, offset=0, **kwargs):
        async def gen():
            yield bytes([msg.id % 256]) * 10
        return gen()
    mock_client.iter_download = _make_iter

    _stub_album_upload(mock_client)

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

    # msg1 streams OK, msg2 download raises → transfer_album returns False.
    def _make_iter(msg, offset=0, **kwargs):
        if msg.id == 602:
            async def bad():
                raise RuntimeError("stream failed")
                yield b""  # unreachable, keeps it a generator
            return bad()
        async def ok():
            yield b"\x00" * 5
        return ok()
    mock_client.iter_download = _make_iter
    mock_client.send_file = AsyncMock()

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

    def _make_iter(msg, offset=0, **kwargs):
        async def gen():
            yield bytes([msg.id % 256]) * 10
        return gen()
    mock_client.iter_download = _make_iter
    # Manual album upload path: make upload_file blow up so the whole upload
    # fails the same way send_file used to.
    async def _boom(path, **kwargs):
        raise RuntimeError("boom")
    mock_client.upload_file = AsyncMock(side_effect=_boom)

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

    # _transfer_media with a job_id streams via iter_download now.
    mock_client.iter_download = _iter_download_stub([b"\x00" * 10], {})
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

    # Stream per-file bytes via iter_download (resumable path keyed by msg).
    # Each msg yields distinct content so artefacts are unambiguous on disk.
    def _make_iter(msg, offset=0, **kwargs):
        async def gen():
            yield bytes([msg.id % 256]) * 10
        return gen()
    mock_client.iter_download = _make_iter

    _stub_album_upload(mock_client)

    # transfer_album computes sha in message order (zip(messages, file_paths)),
    # so side_effect-as-list maps 1:1 to msg1/msg2/msg3.
    with patch(
        "agents.tg_transfer.transfer_engine.compute_sha256",
        side_effect=["sha_a", "dup_sha", "sha_c"],
    ):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2, msg3],
                target_chat="@dst", source_chat="@src", job_id="album_dup",
            )

    assert ok is True
    # upload_file was called with only 2 paths (msg2 removed as duplicate).
    # Photos have no thumb upload, so one upload_file call per file.
    uploaded_paths = [c.args[0] for c in mock_client.upload_file.call_args_list]
    assert len(uploaded_paths) == 2

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

    def _make_iter(msg, offset=0, **kwargs):
        async def gen():
            yield bytes([msg.id % 256]) * 10
        return gen()
    mock_client.iter_download = _make_iter
    req_log = _stub_album_upload(mock_client)

    with patch(
        "agents.tg_transfer.transfer_engine.compute_sha256",
        side_effect=["da", "db"],
    ):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2],
                target_chat="@dst", source_chat="@src", job_id="all_dup",
            )

    assert ok is True
    # Nothing uploaded, no wire requests sent
    mock_client.upload_file.assert_not_called()
    assert req_log == []


@pytest.mark.asyncio
async def test_transfer_album_video_sends_per_file_attributes_and_thumb(
    engine_with_media_db, mock_client, media_db, tmp_path
):
    """Regression: album video upload MUST forward per-file
    DocumentAttributeVideo (duration/w/h) AND thumb to Telegram.

    Telethon's `send_file(file=list, attributes=..., thumb=...)` silently drops
    both in `_send_album` (verified against telethon==1.43.1: `_send_album`
    calls `_file_to_media` without them). For small album videos the TG server
    auto-probe compensates, but for large uploads (~200MB+) the probe fails,
    leaving the bogus `DocumentAttributeVideo(0, 1, 1)` Telethon synthesises —
    rendering the message as 0:00 with no preview.

    Fix: bypass `send_file(list=...)` with a manual pipeline
    (upload_file → InputMediaUploadedDocument(attributes, thumb) →
    UploadMediaRequest → InputSingleMedia → SendMultiMediaRequest), so each
    per-file attribute set actually reaches the server."""
    from telethon.tl.types import (
        InputMediaUploadedDocument, DocumentAttributeVideo,
        Document, PhotoSize,
    )
    from telethon.tl.functions.messages import (
        UploadMediaRequest, SendMultiMediaRequest,
    )

    target_entity = MagicMock()

    def _vid_msg(msg_id, grouped_id, duration, w, h, size):
        m = _make_message(msg_id, grouped_id=grouped_id)
        m.photo = None
        m.video = MagicMock()
        m.document = MagicMock()
        m.document.mime_type = "video/mp4"
        m.document.attributes = []
        m.document.thumbs = [MagicMock()]  # signal that a thumb exists
        m.file = MagicMock()
        m.file.duration = duration
        m.file.width = w
        m.file.height = h
        m.file.size = size
        m.file.mime_type = "video/mp4"
        m.file.ext = ".mp4"
        m.file.name = f"video_{msg_id}.mp4"
        return m

    # Small + large video; the regression is that the large one renders as
    # 0:00 when attributes/thumb are dropped.
    msg1 = _vid_msg(701, grouped_id=70, duration=51, w=1280, h=720, size=6_600_000)
    msg2 = _vid_msg(702, grouped_id=70, duration=195, w=720, h=1280, size=205_000_000)

    def _make_iter(msg, offset=0, **kwargs):
        async def gen():
            yield bytes([msg.id % 256]) * 10
        return gen()
    mock_client.iter_download = _make_iter

    # _download_tg_thumb writes a real JPEG so the fallback extract_video_thumb
    # path is not triggered and the upload code sees a non-None thumb path.
    async def _dl_media(message, file=None, **kwargs):
        if file is not None:
            with open(file, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xe0")  # JPEG magic
            return file
        return None
    mock_client.download_media = AsyncMock(side_effect=_dl_media)

    upload_calls = []

    async def _upload(path, **kwargs):
        upload_calls.append(path)
        handle = MagicMock(name=f"fh_{len(upload_calls)}")
        return handle
    mock_client.upload_file = AsyncMock(side_effect=_upload)

    # If the current (broken) code still reaches send_file, let it complete
    # cleanly so the real assertion (send_file MUST NOT be called) is what
    # fires — not a spurious SQLite type error from MagicMock id fields.
    mock_client.send_file = AsyncMock(
        return_value=[MagicMock(id=9000), MagicMock(id=9001)],
    )

    req_log = []

    async def _call(request):
        req_log.append(request)
        if isinstance(request, UploadMediaRequest):
            # Real Document TL object so tl_utils.get_input_media can cast it
            # back to InputMediaDocument.
            r = MagicMock()
            r.document = Document(
                id=7_000_000 + len(req_log),
                access_hash=1,
                file_reference=b"\x00",
                date=None,
                mime_type="video/mp4",
                size=0,
                dc_id=2,
                attributes=[],
                thumbs=[],
            )
            return r
        if isinstance(request, SendMultiMediaRequest):
            upd = MagicMock()
            upd.updates = []
            return upd
        return MagicMock()
    mock_client.side_effect = _call

    # Per-file ffprobe metadata returned by path basename suffix.
    async def _ffprobe(path):
        if "701" in os.path.basename(path):
            return {"duration": 51, "width": 1280, "height": 720}
        if "702" in os.path.basename(path):
            return {"duration": 195, "width": 720, "height": 1280}
        return None
    # _get_response_message normally builds Message objects from Updates;
    # for the test we stub it so media_db.mark_uploaded isn't fussy.
    def _stub_get_response_message(random_ids, result, entity):
        return [MagicMock(id=5000 + i) for i in range(len(random_ids))]
    mock_client._get_response_message = _stub_get_response_message

    with patch(
        "agents.tg_transfer.transfer_engine.ffprobe_metadata",
        new=AsyncMock(side_effect=_ffprobe),
    ):
        with patch(
            "agents.tg_transfer.transfer_engine.compute_sha256",
            side_effect=lambda p: f"sha-{os.path.basename(p)}",
        ):
            with patch(
                "agents.tg_transfer.transfer_engine.compute_phash_video",
                new=AsyncMock(return_value=None),
            ):
                ok = await engine_with_media_db.transfer_album(
                    target_entity, [msg1, msg2],
                    target_chat="@dst", source_chat="@src",
                    job_id="job-album-video",
                )

    assert ok is True

    # The whole point: never use send_file(list) — it drops per-file
    # attributes/thumb in Telethon's _send_album path.
    assert not mock_client.send_file.called, (
        "album upload must NOT use send_file(list=) (Telethon drops "
        "attributes/thumb). Use UploadMediaRequest + SendMultiMediaRequest."
    )

    upload_reqs = [r for r in req_log if isinstance(r, UploadMediaRequest)]
    send_reqs = [r for r in req_log if isinstance(r, SendMultiMediaRequest)]
    assert len(upload_reqs) == 2, (
        f"expected 2 UploadMediaRequest (one per file), got {len(upload_reqs)}"
    )
    assert len(send_reqs) == 1, (
        f"expected 1 SendMultiMediaRequest, got {len(send_reqs)}"
    )

    seen_dims = []
    for req in upload_reqs:
        assert isinstance(req.media, InputMediaUploadedDocument), (
            f"expected InputMediaUploadedDocument, got {type(req.media).__name__}"
        )
        assert req.media.thumb is not None, (
            "album video thumb must be forwarded — dropping it causes the "
            "no-preview regression"
        )
        vids = [a for a in (req.media.attributes or [])
                if isinstance(a, DocumentAttributeVideo)]
        assert vids, "missing DocumentAttributeVideo on album video upload"
        v = vids[0]
        # Must not be the Telethon placeholder (0, 1, 1) that renders as 0:00.
        assert v.duration > 0, f"duration dropped (got {v.duration})"
        assert v.w > 1 and v.h > 1, f"dims dropped (got {v.w}x{v.h})"
        seen_dims.append((int(v.duration), v.w, v.h))

    # Each file's actual per-file metadata must appear (proving attributes are
    # not collapsed to a single shared value).
    assert (51, 1280, 720) in seen_dims
    assert (195, 720, 1280) in seen_dims

    assert len(send_reqs[0].multi_media) == 2


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
    req_log = _stub_album_upload(mock_client)

    result = await engine.transfer_album(target_entity, [msg1, msg2])

    assert result is True
    # One SendMultiMediaRequest per album upload.
    from telethon.tl.functions.messages import SendMultiMediaRequest
    assert sum(1 for r in req_log if isinstance(r, SendMultiMediaRequest)) == 1


# ---- Phase 1b: _download_with_resume ----


def _iter_download_stub(chunks: list[bytes], capture: dict):
    """Fake client.iter_download that records the offset it was called with
    and yields the given chunks asynchronously."""
    def make(*args, **kwargs):
        capture["offset"] = kwargs.get("offset", 0)
        capture["args"] = args
        capture["kwargs"] = kwargs

        async def gen():
            for c in chunks:
                yield c
        return gen()
    return make


@pytest.mark.asyncio
async def test_download_with_resume_fresh_starts_at_offset_zero(engine, mock_client, db, tmp_path):
    """No prior partial state → iter_download called with offset=0 and file
    contains all streamed bytes."""
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [700])
    msg = _make_message(700)

    capture = {}
    mock_client.iter_download = _iter_download_stub([b"A" * 100, b"B" * 50], capture)

    dest = str(tmp_path / "700.dat")
    path = await engine._download_with_resume(
        msg, dest, job_id=job_id, message_id=700,
    )

    assert capture["offset"] == 0
    assert os.path.getsize(path) == 150
    with open(path, "rb") as f:
        assert f.read() == b"A" * 100 + b"B" * 50


@pytest.mark.asyncio
async def test_download_with_resume_continues_from_stored_offset(engine, mock_client, db, tmp_path):
    """Partial file + stored byte count → iter_download called with offset=
    downloaded_bytes and new chunks are appended, not overwriting."""
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [701])

    dest = str(tmp_path / "701.dat")
    with open(dest, "wb") as f:
        f.write(b"X" * 200)
    await db.set_partial(job_id, 701, dest, 200)

    msg = _make_message(701)
    capture = {}
    mock_client.iter_download = _iter_download_stub([b"Y" * 80], capture)

    path = await engine._download_with_resume(
        msg, dest, job_id=job_id, message_id=701,
    )

    assert capture["offset"] == 200
    assert os.path.getsize(path) == 280
    with open(path, "rb") as f:
        assert f.read() == b"X" * 200 + b"Y" * 80


@pytest.mark.asyncio
async def test_download_with_resume_flushes_to_db_every_flush_bytes(engine, mock_client, db, tmp_path):
    """Every flush_bytes chunk boundary must update db.downloaded_bytes so a
    crash mid-stream can resume near the latest flush."""
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [702])
    msg = _make_message(702)

    capture = {}
    mock_client.iter_download = _iter_download_stub(
        [b"a" * 100, b"b" * 100, b"c" * 100], capture
    )

    dest = str(tmp_path / "702.dat")
    path = await engine._download_with_resume(
        msg, dest, job_id=job_id, message_id=702, flush_bytes=100,
    )

    row = await db.get_message(job_id, 702)
    assert row["downloaded_bytes"] == 300
    assert row["partial_path"] == path


@pytest.mark.asyncio
async def test_download_with_resume_truncates_when_disk_less_than_db(engine, mock_client, db, tmp_path):
    """If on-disk file is smaller than DB-recorded bytes (flush succeeded but
    tail was lost), resume from on-disk size, not the DB lie — avoids asking
    TG for bytes past EOF."""
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [703])

    dest = str(tmp_path / "703.dat")
    with open(dest, "wb") as f:
        f.write(b"Z" * 50)
    await db.set_partial(job_id, 703, dest, 200)

    msg = _make_message(703)
    capture = {}
    mock_client.iter_download = _iter_download_stub([b"W" * 20], capture)

    path = await engine._download_with_resume(
        msg, dest, job_id=job_id, message_id=703,
    )

    assert capture["offset"] == 50
    assert os.path.getsize(path) == 70


# ---- Phase 1b slice 3: transfer_album resumable ----


@pytest.mark.asyncio
async def test_transfer_album_streams_via_iter_download(
    engine_with_media_db, mock_client, media_db, db, tmp_path
):
    """Album download path must now stream via iter_download so each file is
    resumable, not use the atomic download_media."""
    target_entity = MagicMock()
    msg1 = _make_message(801, text="cap", grouped_id=80)
    msg2 = _make_message(802, grouped_id=80)

    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [801, 802], grouped_ids={801: 80, 802: 80})

    # Track which messages iter_download was called with.
    iter_calls = []
    def make_iter_download(msg, offset=0, **kwargs):
        iter_calls.append((msg.id, offset))
        async def gen():
            yield b"\xAB" * 8
        return gen()
    mock_client.iter_download = make_iter_download
    _stub_album_upload(mock_client)

    with patch("agents.tg_transfer.transfer_engine.compute_sha256",
               side_effect=["sha_a", "sha_b"]):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2],
                target_chat="@dst", source_chat="@src", job_id=job_id,
            )

    assert ok is True
    called_ids = sorted(c[0] for c in iter_calls)
    assert called_ids == [801, 802]
    # Fresh downloads → offset 0
    for _, offset in iter_calls:
        assert offset == 0


@pytest.mark.asyncio
async def test_transfer_album_resumes_one_file_from_partial(
    engine_with_media_db, mock_client, media_db, db, tmp_path
):
    """If one album file has partial state, its iter_download is called with
    offset=downloaded_bytes while the other starts fresh."""
    target_entity = MagicMock()
    msg1 = _make_message(901, text="cap", grouped_id=90)
    msg2 = _make_message(902, grouped_id=90)

    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [901, 902], grouped_ids={901: 90, 902: 90})

    # Pre-seed msg 901 partial state on disk + in DB.
    partial = str(tmp_path / "downloads" / "901.partial")
    os.makedirs(os.path.dirname(partial), exist_ok=True)
    with open(partial, "wb") as f:
        f.write(b"P" * 123)
    await db.set_partial(job_id, 901, partial, 123)

    iter_calls = {}
    def make_iter_download(msg, offset=0, **kwargs):
        iter_calls[msg.id] = offset
        async def gen():
            yield b"R" * 7
        return gen()
    mock_client.iter_download = make_iter_download
    _stub_album_upload(mock_client)

    with patch("agents.tg_transfer.transfer_engine.compute_sha256",
               side_effect=["sha1", "sha2"]):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2],
                target_chat="@dst", source_chat="@src", job_id=job_id,
            )

    assert ok is True
    assert iter_calls[901] == 123
    assert iter_calls[902] == 0


@pytest.mark.asyncio
async def test_transfer_album_clears_partial_state_on_success(
    engine_with_media_db, mock_client, media_db, db, tmp_path
):
    """After all uploads land, album messages' partial_path + downloaded_bytes
    must be cleared so a future run doesn't try to resume delivered files."""
    target_entity = MagicMock()
    msg1 = _make_message(1001, text="cap", grouped_id=100)
    msg2 = _make_message(1002, grouped_id=100)

    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [1001, 1002], grouped_ids={1001: 100, 1002: 100})

    def make_iter_download(msg, offset=0, **kwargs):
        async def gen():
            yield b"X" * 4
        return gen()
    mock_client.iter_download = make_iter_download
    _stub_album_upload(mock_client)

    with patch("agents.tg_transfer.transfer_engine.compute_sha256",
               side_effect=["sX", "sY"]):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2],
                target_chat="@dst", source_chat="@src", job_id=job_id,
            )

    for mid in (1001, 1002):
        row = await db.get_message(job_id, mid)
        assert row["partial_path"] is None
        assert row["downloaded_bytes"] == 0


# ---- Phase 1b slice 4: ByteBudget gates concurrent downloads ----


@pytest.mark.asyncio
async def test_download_with_resume_reserves_from_byte_budget(
    mock_client, db, tmp_path,
):
    """When engine has a byte_budget, _download_with_resume must reserve
    message.file.size from it before starting and release after finishing —
    so a 1GB global cap really limits total in-flight bytes."""
    from agents.tg_transfer.byte_budget import ByteBudget
    budget = ByteBudget(capacity=1024)
    engine = TransferEngine(
        client=mock_client, db=db, tmp_dir=str(tmp_path / "downloads"),
        retry_limit=2, progress_interval=2, byte_budget=budget,
    )

    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [900])

    msg = _make_message(900)
    msg.file = MagicMock(size=400)  # declared file size

    mock_client.iter_download = _iter_download_stub([b"x" * 400], {})

    assert budget.available == 1024
    dest = str(tmp_path / "900.dat")
    await engine._download_with_resume(
        msg, dest, job_id=job_id, message_id=900,
    )
    assert budget.available == 1024, "budget must be fully released after download"


@pytest.mark.asyncio
async def test_byte_budget_blocks_second_download_until_first_releases(
    mock_client, db, tmp_path,
):
    """Two concurrent downloads of 700 bytes each against a 1000-byte budget:
    the first reserves 700 and starts; the second must wait for the first to
    release before it can begin. Proves the budget actually serialises
    oversubscribing downloads."""
    import asyncio
    from agents.tg_transfer.byte_budget import ByteBudget
    budget = ByteBudget(capacity=1000)
    engine = TransferEngine(
        client=mock_client, db=db, tmp_dir=str(tmp_path / "downloads"),
        retry_limit=2, progress_interval=2, byte_budget=budget,
    )

    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [910, 911])

    # Gate for the first download: it won't yield until `first_gate` is set.
    first_gate = asyncio.Event()
    second_started = asyncio.Event()
    started_order: list[int] = []

    def fake_iter_download(msg, offset=0, **kwargs):
        started_order.append(msg.id)

        async def gen():
            if msg.id == 910:
                await first_gate.wait()
            else:
                second_started.set()
            yield b"y" * 700
        return gen()

    mock_client.iter_download = fake_iter_download

    m1 = _make_message(910)
    m1.file = MagicMock(size=700)
    m2 = _make_message(911)
    m2.file = MagicMock(size=700)
    d1 = str(tmp_path / "910.dat")
    d2 = str(tmp_path / "911.dat")

    t1 = asyncio.create_task(engine._download_with_resume(
        m1, d1, job_id=job_id, message_id=910,
    ))
    t2 = asyncio.create_task(engine._download_with_resume(
        m2, d2, job_id=job_id, message_id=911,
    ))
    await asyncio.sleep(0.05)

    # First has started its iter_download. Second must be blocked on budget,
    # so its iter_download must NOT have been called yet.
    assert 910 in started_order
    assert 911 not in started_order
    assert not second_started.is_set()

    # Release first → second can proceed.
    first_gate.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
    assert 911 in started_order


@pytest.mark.asyncio
async def test_byte_budget_released_on_exception(
    mock_client, db, tmp_path,
):
    """If the download raises mid-stream, the reserved bytes must go back to
    the budget — otherwise a few failed downloads would starve the pool."""
    from agents.tg_transfer.byte_budget import ByteBudget
    budget = ByteBudget(capacity=500)
    engine = TransferEngine(
        client=mock_client, db=db, tmp_dir=str(tmp_path / "downloads"),
        retry_limit=2, progress_interval=2, byte_budget=budget,
    )
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [920])

    def fake_iter(msg, offset=0, **kwargs):
        async def gen():
            raise RuntimeError("stream error")
            yield b""  # unreachable
        return gen()
    mock_client.iter_download = fake_iter

    msg = _make_message(920)
    msg.file = MagicMock(size=300)
    dest = str(tmp_path / "920.dat")

    with pytest.raises(RuntimeError):
        await engine._download_with_resume(
            msg, dest, job_id=job_id, message_id=920,
        )
    assert budget.available == 500


@pytest.mark.asyncio
async def test_no_byte_budget_behaves_as_before(engine, mock_client, db, tmp_path):
    """Regression: engines constructed without byte_budget work exactly as
    before (no gating, no exceptions)."""
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [930])
    msg = _make_message(930)
    mock_client.iter_download = _iter_download_stub([b"z" * 50], {})
    dest = str(tmp_path / "930.dat")
    path = await engine._download_with_resume(
        msg, dest, job_id=job_id, message_id=930,
    )
    assert os.path.getsize(path) == 50


# ---- #3 slice 1: static per-message size threshold ----


@pytest.mark.asyncio
async def test_transfer_single_skips_message_over_size_limit(
    engine_with_media_db, mock_client, db, tmp_path,
):
    """When config[size_limit_mb] is set and a message's declared file size
    exceeds it, _transfer_media must NOT download — return over_limit=True so
    run_batch can mark it 'skipped' without wasting bandwidth."""
    await db.set_config("size_limit_mb", "100")  # 100 MB

    target_entity = MagicMock()
    msg = _make_video_message(2000, text="huge file")
    msg.file = MagicMock(size=150 * 1024 * 1024)  # 150 MB > 100 MB

    # iter_download / send_file must not be touched — fail hard if they are.
    mock_client.iter_download = MagicMock(
        side_effect=AssertionError("must not download an over-limit file")
    )
    mock_client.send_file = AsyncMock(
        side_effect=AssertionError("must not upload an over-limit file")
    )

    result = await engine_with_media_db._transfer_media(
        target_entity, msg,
        target_chat="@d", source_chat="@s", job_id=None,
    )
    assert result["ok"] is False
    assert result.get("over_limit") is True


@pytest.mark.asyncio
async def test_transfer_single_under_limit_proceeds(
    engine, mock_client, db, tmp_path,
):
    """A file well under the limit must transfer as usual."""
    await db.set_config("size_limit_mb", "100")

    target_entity = MagicMock()
    msg = _make_message(2001, text="small")
    msg.file = MagicMock(size=10 * 1024 * 1024)  # 10 MB

    small_path = str(tmp_path / "downloads" / "small.dat")
    os.makedirs(os.path.dirname(small_path), exist_ok=True)
    with open(small_path, "wb") as f:
        f.write(b"x" * 10)
    mock_client.download_media = AsyncMock(return_value=small_path)
    mock_client.send_file = AsyncMock(return_value=MagicMock(id=1))

    with patch("agents.tg_transfer.transfer_engine.compute_sha256", return_value="s"):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            result = await engine._transfer_media(
                target_entity, msg,
                target_chat="@d", source_chat="@s", job_id=None,
            )
    assert result["ok"] is True
    assert not result.get("over_limit")


@pytest.mark.asyncio
async def test_transfer_single_no_limit_set_is_no_op(
    engine, mock_client, db, tmp_path,
):
    """With no size_limit_mb config, behavior must be unchanged (regression)."""
    target_entity = MagicMock()
    msg = _make_message(2002, text="whatever")
    msg.file = MagicMock(size=5 * 1024 * 1024 * 1024)  # 5 GB

    path = str(tmp_path / "downloads" / "big.dat")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * 10)
    mock_client.download_media = AsyncMock(return_value=path)
    mock_client.send_file = AsyncMock(return_value=MagicMock(id=1))

    with patch("agents.tg_transfer.transfer_engine.compute_sha256", return_value="s"):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            result = await engine._transfer_media(
                target_entity, msg,
                target_chat="@d", source_chat="@s", job_id=None,
            )
    assert result["ok"] is True
    assert not result.get("over_limit")


@pytest.mark.asyncio
async def test_run_batch_marks_over_limit_single_as_skipped(
    engine, mock_client, db, tmp_path,
):
    """The batch loop must translate over_limit=True into 'skipped' (not
    'failed') so retry logic doesn't kick in and the job progresses."""
    await db.set_config("size_limit_mb", "50")
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [3000])

    source_entity = MagicMock()
    target_entity = MagicMock()

    big_msg = _make_video_message(3000)
    big_msg.file = MagicMock(size=100 * 1024 * 1024)
    mock_client.get_messages = AsyncMock(return_value=big_msg)

    async def report(text):
        pass

    # Stub the job status transitions so the terminal-cleanup hook doesn't
    # wipe job_messages before we can read per-message status back.
    engine.db.update_job_status = AsyncMock()

    status = await engine.run_batch(job_id, source_entity, target_entity, report)
    assert status == "completed"
    row = await db.get_message(job_id, 3000)
    assert row["status"] == "skipped"


@pytest.mark.asyncio
async def test_size_limit_is_read_dynamically_per_message(
    engine, mock_client, db, tmp_path,
):
    """Changing the config between messages must take effect on the NEXT
    message. Foundation for slice 3 (live threshold changes)."""
    await db.set_config("size_limit_mb", "1000")
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [4000, 4001])

    source_entity = MagicMock()
    target_entity = MagicMock()

    async def fake_get_messages(entity, ids=None):
        m = _make_message(ids)
        m.file = MagicMock(size=100 * 1024 * 1024)  # 100 MB each
        return m

    mock_client.get_messages = fake_get_messages

    async def fake_transfer_single(src, tgt, msg, target_chat="", source_chat="", job_id=None, task_id=None):
        if msg.id == 4000:
            # Lower the limit before msg2 is processed.
            await db.set_config("size_limit_mb", "50")
            return {"ok": True, "dedup": False, "similar": None}
        return {"ok": True, "dedup": False, "similar": None}

    engine.transfer_single = fake_transfer_single

    async def report(text):
        pass

    engine.db.update_job_status = AsyncMock()

    status = await engine.run_batch(job_id, source_entity, target_entity, report)
    assert status == "completed"

    row1 = await db.get_message(job_id, 4000)
    row2 = await db.get_message(job_id, 4001)
    assert row1["status"] == "success"
    assert row2["status"] == "skipped"


# ---- #3 slice 4: cancel currently-downloading over-limit file ----


@pytest.mark.asyncio
async def test_download_aborts_when_threshold_lowered_mid_stream(
    engine, mock_client, db, tmp_path,
):
    """If user lowers size_limit_mb while a file is already downloading and
    the in-flight bytes exceed the new limit, _download_with_resume must
    raise OverSizeLimit at the next flush boundary (no indefinite streaming).
    """
    from agents.tg_transfer.transfer_engine import OverSizeLimit

    # Start with a generous limit so download begins.
    await db.set_config("size_limit_mb", "1000")
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [6000])
    msg = _make_message(6000)
    msg.file = MagicMock(size=300)

    # iter_download emits in 100-byte chunks. Between chunks, we lower the
    # limit to something the running download already exceeds.
    async def fake_iter_download(message, offset=0, **kwargs):
        yield b"a" * 100
        # Mid-stream: user lowers threshold to 0 MB ... no wait, needs to be
        # smaller than downloaded_bytes, which in bytes is 100. Limit is in MB
        # so 1 MB = 1,048,576 B > 100. To make 100 bytes exceed, limit must
        # be 0 MB — but that's treated as "no limit". Use a non-zero tiny
        # limit by patching size_limit_bytes directly in the engine hook.
        # Simpler: make flush_bytes very small and bump the chunk size.
        yield b"b" * 100
        yield b"c" * 100

    mock_client.iter_download = fake_iter_download

    # Patch _size_limit_bytes so the first call returns huge, subsequent ones
    # return a value smaller than downloaded_bytes. Threaded through an
    # iterator so we don't hardcode call count.
    limits = iter([1024 * 1024 * 1024, 1024 * 1024 * 1024, 50, 50, 50, 50])

    async def fake_limit():
        try:
            return next(limits)
        except StopIteration:
            return 50

    engine._size_limit_bytes = fake_limit

    dest = str(tmp_path / "6000.dat")
    with pytest.raises(OverSizeLimit):
        await engine._download_with_resume(
            msg, dest, job_id=job_id, message_id=6000, flush_bytes=100,
        )


@pytest.mark.asyncio
async def test_run_batch_catches_oversize_mid_stream_and_skips(
    engine, mock_client, db, tmp_path,
):
    """When _transfer_media bubbles OverSizeLimit up, run_batch must mark the
    message 'skipped' (not retry it) — retrying would just hit the same wall."""
    from agents.tg_transfer.transfer_engine import OverSizeLimit

    await db.set_config("size_limit_mb", "1000")
    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [6100])

    msg = _make_message(6100)
    msg.file = MagicMock(size=10 * 1024 * 1024)

    mock_client.get_messages = AsyncMock(return_value=msg)

    async def boom(source, target, message, target_chat="", source_chat="", job_id=None, task_id=None):
        raise OverSizeLimit("downloaded exceeds live limit")

    engine.transfer_single = boom

    async def report(text):
        pass

    engine.db.update_job_status = AsyncMock()

    status = await engine.run_batch(job_id, MagicMock(), MagicMock(), report)
    assert status == "completed"
    row = await db.get_message(job_id, 6100)
    assert row["status"] == "skipped"


# ---- #3 slice 2: album SUM size threshold ----


@pytest.mark.asyncio
async def test_check_album_over_limit_when_sum_exceeds(engine, db):
    """Engine exposes a policy check: sum of album file sizes vs the
    configured size_limit_mb. Callers (run_batch, _handle_single) use this
    to decide whether to skip the whole album before downloading."""
    await db.set_config("size_limit_mb", "200")
    msg1 = _make_message(5001, grouped_id=500)
    msg2 = _make_message(5002, grouped_id=500)
    msg3 = _make_message(5003, grouped_id=500)
    for m, sz in [(msg1, 80), (msg2, 80), (msg3, 80)]:  # 240 MB > 200
        m.file = MagicMock(size=sz * 1024 * 1024)
    over = await engine._album_over_limit([msg1, msg2, msg3])
    assert over is True


@pytest.mark.asyncio
async def test_check_album_over_limit_when_sum_under(engine, db):
    await db.set_config("size_limit_mb", "200")
    msg1 = _make_message(5011, grouped_id=501)
    msg2 = _make_message(5012, grouped_id=501)
    for m in (msg1, msg2):
        m.file = MagicMock(size=50 * 1024 * 1024)  # 100 MB < 200
    over = await engine._album_over_limit([msg1, msg2])
    assert over is False


@pytest.mark.asyncio
async def test_check_album_over_limit_no_config_is_never_over(engine, db):
    """No size_limit_mb set → never over limit (keeps legacy behavior)."""
    msg1 = _make_message(5021, grouped_id=502)
    msg1.file = MagicMock(size=5 * 1024 * 1024 * 1024)  # 5 GB
    assert await engine._album_over_limit([msg1]) is False


@pytest.mark.asyncio
async def test_transfer_album_under_sum_limit_proceeds(
    engine_with_media_db, mock_client, db, tmp_path,
):
    """An album well under the sum limit transfers normally."""
    await db.set_config("size_limit_mb", "200")

    target_entity = MagicMock()
    msg1 = _make_message(5101, text="cap", grouped_id=510)
    msg2 = _make_message(5102, grouped_id=510)
    for m, sz in [(msg1, 50), (msg2, 50)]:  # 100 MB < 200
        m.file = MagicMock(size=sz * 1024 * 1024)

    def _make_iter(msg, offset=0, **kwargs):
        async def gen():
            yield bytes([msg.id % 256]) * 10
        return gen()
    mock_client.iter_download = _make_iter
    _stub_album_upload(mock_client)

    with patch(
        "agents.tg_transfer.transfer_engine.compute_sha256",
        side_effect=["a", "b"],
    ):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2],
                target_chat="@dst", source_chat="@src", job_id="ok_album",
            )
    # Existing contract: success returns True
    assert ok is True


@pytest.mark.asyncio
async def test_run_batch_marks_over_limit_album_as_skipped(
    engine, mock_client, db, tmp_path,
):
    """run_batch must recognise album-level over_limit and mark every message
    in the group as 'skipped' (not 'failed') so retry doesn't fire."""
    await db.set_config("size_limit_mb", "100")

    job_id = await db.create_job("@s", "@d", "batch")
    await db.add_messages(job_id, [5201, 5202], grouped_ids={5201: 520, 5202: 520})

    async def fake_get_messages(entity, ids=None):
        m = _make_message(ids, grouped_id=520)
        m.file = MagicMock(size=80 * 1024 * 1024)  # 80+80 = 160 MB > 100
        return m

    mock_client.get_messages = fake_get_messages

    async def report(text):
        pass

    engine.db.update_job_status = AsyncMock()

    status = await engine.run_batch(job_id, MagicMock(), MagicMock(), report)
    assert status == "completed"
    for mid in (5201, 5202):
        row = await db.get_message(job_id, mid)
        assert row["status"] == "skipped", (
            f"msg {mid} should be skipped due to album sum over limit"
        )


@pytest.mark.asyncio
async def test_transfer_album_no_limit_set_is_no_op(
    engine_with_media_db, mock_client, db, tmp_path,
):
    """No size_limit_mb config → existing album behavior untouched."""
    target_entity = MagicMock()
    msg1 = _make_message(5301, text="cap", grouped_id=530)
    msg2 = _make_message(5302, grouped_id=530)
    # Huge but no limit set.
    for m in (msg1, msg2):
        m.file = MagicMock(size=5 * 1024 * 1024 * 1024)

    def _make_iter(msg, offset=0, **kwargs):
        async def gen():
            yield b"\x00" * 5
        return gen()
    mock_client.iter_download = _make_iter
    _stub_album_upload(mock_client)

    with patch(
        "agents.tg_transfer.transfer_engine.compute_sha256",
        side_effect=["n1", "n2"],
    ):
        with patch("agents.tg_transfer.transfer_engine.compute_phash", return_value=None):
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2],
                target_chat="@dst", source_chat="@src", job_id="nolim",
            )
    assert ok is True


class TestUploadFilenameExtension:
    """Regression: transferred files were arriving with `.dat` filenames because
    the local temp path used a hardcoded `.dat` suffix, and Telethon uses the
    path basename as the uploaded filename. Recipients saw every photo/video as
    a generic .dat file. Fix: derive the extension from the source message."""

    @pytest.mark.asyncio
    async def test_photo_upload_ends_with_jpg(self, engine, mock_client, tmp_path):
        target_entity = MagicMock()
        msg = _make_message(7001, text="pic", media=True)
        msg.document = None
        msg.file = MagicMock()
        msg.file.ext = ".jpg"
        msg.file.name = None

        # Record whatever path send_file / download_media see.
        captured = {}

        async def fake_download(message, file=None, **kw):
            captured["download_file"] = file
            os.makedirs(os.path.dirname(file), exist_ok=True)
            with open(file, "wb") as fh:
                fh.write(b"\x89PNG\x00")
            return file

        mock_client.download_media = AsyncMock(side_effect=fake_download)
        mock_client.send_file = AsyncMock(return_value=MagicMock(id=1))

        with patch(
            "agents.tg_transfer.transfer_engine.compute_sha256", return_value="p1",
        ), patch(
            "agents.tg_transfer.transfer_engine.compute_phash", return_value=None,
        ):
            await engine.transfer_single(MagicMock(), target_entity, msg)

        uploaded_path = mock_client.send_file.call_args.args[1]
        assert uploaded_path.endswith(".jpg"), (
            f"photo upload path should end in .jpg, got {uploaded_path!r} "
            f"— Telegram needs a real image extension to render inline"
        )
        assert not captured["download_file"].endswith(".dat")

    @pytest.mark.asyncio
    async def test_video_upload_ends_with_mp4(self, engine, mock_client, tmp_path):
        target_entity = MagicMock()
        msg = _make_video_message(7002, text="clip")
        msg.document = MagicMock()
        msg.document.thumbs = None
        msg.file = MagicMock()
        msg.file.ext = ".mp4"
        msg.file.name = None
        msg.file.width = 640
        msg.file.height = 480
        msg.file.duration = 5

        async def fake_download(message, file=None, **kw):
            os.makedirs(os.path.dirname(file), exist_ok=True)
            with open(file, "wb") as fh:
                fh.write(b"\x00")
            return file

        mock_client.download_media = AsyncMock(side_effect=fake_download)
        mock_client.send_file = AsyncMock(return_value=MagicMock(id=2))

        with patch(
            "agents.tg_transfer.transfer_engine.ffprobe_metadata",
            new_callable=AsyncMock, return_value=None,
        ), patch(
            "agents.tg_transfer.transfer_engine.compute_sha256", return_value="v1",
        ), patch(
            "agents.tg_transfer.transfer_engine.compute_phash_video",
            new_callable=AsyncMock, return_value=None,
        ):
            await engine.transfer_single(MagicMock(), target_entity, msg)

        uploaded_path = mock_client.send_file.call_args.args[1]
        assert uploaded_path.endswith(".mp4"), (
            f"video upload path should end in .mp4, got {uploaded_path!r}"
        )

    @pytest.mark.asyncio
    async def test_document_preserves_original_extension(
        self, engine, mock_client, tmp_path,
    ):
        """For arbitrary documents, keep the source's original extension so
        recipients see `report.pdf` rather than `report.dat`."""
        target_entity = MagicMock()
        msg = _make_message(7003, text=None, media=True)
        msg.photo = None
        msg.video = None
        msg.document = MagicMock()
        msg.document.thumbs = None
        msg.file = MagicMock()
        msg.file.ext = ".pdf"
        msg.file.name = "report.pdf"

        async def fake_download(message, file=None, **kw):
            os.makedirs(os.path.dirname(file), exist_ok=True)
            with open(file, "wb") as fh:
                fh.write(b"%PDF-")
            return file

        mock_client.download_media = AsyncMock(side_effect=fake_download)
        mock_client.send_file = AsyncMock(return_value=MagicMock(id=3))

        with patch(
            "agents.tg_transfer.transfer_engine.compute_sha256", return_value="d1",
        ):
            await engine.transfer_single(MagicMock(), target_entity, msg)

        uploaded_path = mock_client.send_file.call_args.args[1]
        assert uploaded_path.endswith(".pdf"), (
            f"document upload path should preserve .pdf, got {uploaded_path!r}"
        )

    @pytest.mark.asyncio
    async def test_album_photos_end_with_jpg(
        self, engine_with_media_db, mock_client, tmp_path,
    ):
        target_entity = MagicMock()
        msg1 = _make_message(7010, text="album", grouped_id=70)
        msg2 = _make_message(7011, grouped_id=70)
        for m in (msg1, msg2):
            m.file = MagicMock(ext=".jpg", name=None)

        def _make_iter(msg, offset=0, **kwargs):
            async def gen():
                yield b"\x89PNG"
            return gen()
        mock_client.iter_download = _make_iter
        _stub_album_upload(mock_client)

        with patch(
            "agents.tg_transfer.transfer_engine.compute_sha256",
            side_effect=["a1", "a2"],
        ), patch(
            "agents.tg_transfer.transfer_engine.compute_phash", return_value=None,
        ):
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2],
                target_chat="@dst", source_chat="@src", job_id="ext-album",
            )

        assert ok is True
        # Photos → one upload_file call per file (no thumb), path is arg 0.
        uploaded_paths = [c.args[0] for c in mock_client.upload_file.call_args_list]
        assert len(uploaded_paths) == 2
        for p in uploaded_paths:
            assert p.endswith(".jpg"), (
                f"album file path should end in .jpg, got {p!r}"
            )

    def test_derive_upload_ext_uses_metadata(self):
        """Extension comes from Telethon's message.file.ext (metadata-derived)."""
        from agents.tg_transfer.transfer_engine import _derive_upload_ext
        msg = MagicMock()
        msg.file = MagicMock(ext=".webp")
        assert _derive_upload_ext(msg) == ".webp"

    def test_derive_upload_ext_no_metadata_returns_empty(self):
        """When metadata has no ext, return empty string — not .dat."""
        from agents.tg_transfer.transfer_engine import _derive_upload_ext
        msg = MagicMock()
        msg.file = MagicMock(ext=None)
        result = _derive_upload_ext(msg)
        assert result == ""


# ---------------------------------------------------------------------------
# Phase 4: pre_dedup_by_thumb
# ---------------------------------------------------------------------------

def _make_media_message(msg_id, text, file_type, file_size, duration=None):
    """Shape a Telethon-like message for _pre_dedup_by_thumb input."""
    msg = MagicMock()
    msg.id = msg_id
    msg.text = text
    msg.message = text
    msg.media = MagicMock()
    msg.photo = MagicMock() if file_type == "photo" else None
    msg.video = MagicMock() if file_type == "video" else None
    msg.document = None
    msg.sticker = None
    msg.voice = None
    msg.file = MagicMock(size=file_size, duration=duration)
    return msg


class TestPreDedupByThumb:
    @pytest.mark.asyncio
    async def test_no_media_db_returns_hit_false(self, engine):
        """Without a media_db wired, thumb dedup is a no-op."""
        msg = _make_media_message(1, "cap", "photo", 1234)
        result = await engine._pre_dedup_by_thumb(
            msg, target_chat="@t", source_chat="@s", job_id="j",
        )
        assert result == {"hit": False}

    @pytest.mark.asyncio
    async def test_document_type_skips(self, engine_with_media_db):
        """document/voice have no usable thumb index — short-circuit."""
        msg = _make_media_message(1, "cap", "photo", 1234)
        msg.photo = None  # downgrade to document
        msg.document = MagicMock()
        result = await engine_with_media_db._pre_dedup_by_thumb(
            msg, target_chat="@t", source_chat="@s", job_id="j",
        )
        assert result == {"hit": False}

    @pytest.mark.asyncio
    async def test_no_thumb_phash_returns_hit_false(self, engine_with_media_db):
        """If the thumb can't be hashed (no imagehash, decode error, etc.) we
        must fall through — not crash."""
        msg = _make_media_message(1, "cap", "photo", 1234)
        with patch(
            "agents.tg_transfer.transfer_engine.download_thumb_and_phash",
            new_callable=AsyncMock, return_value=None,
        ):
            result = await engine_with_media_db._pre_dedup_by_thumb(
                msg, target_chat="@t", source_chat="@s", job_id="j",
            )
        assert result == {"hit": False}

    @pytest.mark.asyncio
    async def test_no_candidates_returns_hit_false(self, engine_with_media_db):
        """Thumb hashed fine but the target index has no match — fall through."""
        msg = _make_media_message(1, "cap", "photo", 1234)
        with patch(
            "agents.tg_transfer.transfer_engine.download_thumb_and_phash",
            new_callable=AsyncMock, return_value="abcd",
        ):
            result = await engine_with_media_db._pre_dedup_by_thumb(
                msg, target_chat="@t", source_chat="@s", job_id="j",
            )
        assert result == {"hit": False}

    @pytest.mark.asyncio
    async def test_strict_match_auto_dedups_and_upgrades(
        self, engine_with_media_db, media_db,
    ):
        """All four fields match (thumb + caption + size + duration) → auto
        skip upload, promote candidate row from thumb_only to full."""
        cand_id = await media_db.insert_thumb_record(
            thumb_phash="abcd", file_type="photo", file_size=1234,
            caption="cap", duration=None,
            target_chat="@t", target_msg_id=999,
        )
        msg = _make_media_message(1, "cap", "photo", 1234)

        with patch(
            "agents.tg_transfer.transfer_engine.download_thumb_and_phash",
            new_callable=AsyncMock, return_value="abcd",
        ):
            result = await engine_with_media_db._pre_dedup_by_thumb(
                msg, target_chat="@t", source_chat="@s", job_id="j1",
            )

        assert result == {"hit": True, "dedup": True}
        row = await media_db.get_media(cand_id)
        assert row["trust"] == "full"
        assert row["verified_by"] == "metadata"
        # sha256/phash must stay NULL — we never downloaded the file.
        assert row["sha256"] is None
        assert row["phash"] is None
        # No ambiguous row queued.
        assert await media_db.list_pending_dedup_by_job("j1") == []

    @pytest.mark.asyncio
    async def test_ambiguous_when_caption_differs(
        self, engine_with_media_db, media_db,
    ):
        """Thumb matches but caption differs → enqueue for user resolution."""
        await media_db.insert_thumb_record(
            thumb_phash="abcd", file_type="photo", file_size=1234,
            caption="different caption", duration=None,
            target_chat="@t", target_msg_id=999,
        )
        msg = _make_media_message(1, "my caption", "photo", 1234)

        with patch(
            "agents.tg_transfer.transfer_engine.download_thumb_and_phash",
            new_callable=AsyncMock, return_value="abcd",
        ):
            result = await engine_with_media_db._pre_dedup_by_thumb(
                msg, target_chat="@t", source_chat="@s", job_id="j1",
            )

        assert result == {"hit": True, "ambiguous": True}
        pending = await media_db.list_pending_dedup_by_job("j1")
        assert len(pending) == 1
        assert pending[0]["source_msg_id"] == 1
        assert pending[0]["candidate_target_msg_ids"] == [999]
        assert pending[0]["reason"] == "thumb_match_metadata_mismatch"

    @pytest.mark.asyncio
    async def test_ambiguous_when_file_size_differs(
        self, engine_with_media_db, media_db,
    ):
        """Same thumb + caption but different file_size — still ambiguous,
        because a re-encode would flip size while keeping caption identical."""
        await media_db.insert_thumb_record(
            thumb_phash="abcd", file_type="photo", file_size=999,
            caption="cap", duration=None,
            target_chat="@t", target_msg_id=888,
        )
        msg = _make_media_message(1, "cap", "photo", 1234)

        with patch(
            "agents.tg_transfer.transfer_engine.download_thumb_and_phash",
            new_callable=AsyncMock, return_value="abcd",
        ):
            result = await engine_with_media_db._pre_dedup_by_thumb(
                msg, target_chat="@t", source_chat="@s", job_id="j1",
            )

        assert result == {"hit": True, "ambiguous": True}
        pending = await media_db.list_pending_dedup_by_job("j1")
        assert len(pending) == 1
        assert ".dat" not in result


class TestPremiumChunkTuning:
    """Premium accounts get larger download chunks for ~2x throughput. The
    flag is read from the client on each call via getattr(..., False) so a
    missing attribute falls back to non-premium — important because some
    test paths construct a TransferEngine with a bare MagicMock client."""

    def test_non_premium_defaults(self, engine, mock_client):
        # client has no premium_account attr at all → treated as non-premium.
        if hasattr(mock_client, "premium_account"):
            del mock_client.premium_account
        assert engine._download_request_size() == 512 * 1024

    def test_premium_uses_max_chunks(self, engine, mock_client):
        mock_client.premium_account = True
        assert engine._download_request_size() == 1024 * 1024

    def test_explicit_false_matches_non_premium(self, engine, mock_client):
        mock_client.premium_account = False
        assert engine._download_request_size() == 512 * 1024


class TestDetectFileType:
    """Regression guard: 'send as file' videos arrive with message.video=None
    and only message.document set (no re-encoding). Without classifying those
    as video, the upload path skipped ffprobe + DocumentAttributeVideo — TG
    then rendered the upload as a grey file tile with 0:00 duration and no
    aspect ratio. Detection must look past message.video into the document's
    MIME type and attributes."""

    def _build_doc_msg(self, mime_type=None, attrs=None):
        msg = MagicMock()
        msg.photo = None
        msg.video = None
        doc = MagicMock()
        doc.mime_type = mime_type
        doc.attributes = attrs or []
        msg.document = doc
        return msg

    def test_native_photo(self):
        from agents.tg_transfer.transfer_engine import TransferEngine
        msg = MagicMock()
        msg.photo = MagicMock()
        assert TransferEngine._detect_file_type(msg) == "photo"

    def test_native_video(self):
        from agents.tg_transfer.transfer_engine import TransferEngine
        msg = MagicMock()
        msg.photo = None
        msg.video = MagicMock()
        assert TransferEngine._detect_file_type(msg) == "video"

    def test_video_sent_as_file_by_mime(self):
        """The original bug — mp4 uploaded via 'send as file'."""
        from agents.tg_transfer.transfer_engine import TransferEngine
        msg = self._build_doc_msg(mime_type="video/mp4")
        assert TransferEngine._detect_file_type(msg) == "video"

    def test_video_sent_as_file_by_attribute(self):
        """Some clients set DocumentAttributeVideo even without video/* MIME."""
        from agents.tg_transfer.transfer_engine import TransferEngine
        from telethon.tl.types import DocumentAttributeVideo
        attrs = [DocumentAttributeVideo(duration=30, w=1920, h=1080)]
        msg = self._build_doc_msg(mime_type="application/octet-stream", attrs=attrs)
        assert TransferEngine._detect_file_type(msg) == "video"

    def test_plain_document_stays_document(self):
        from agents.tg_transfer.transfer_engine import TransferEngine
        msg = self._build_doc_msg(mime_type="application/pdf")
        assert TransferEngine._detect_file_type(msg) == "document"

    def test_no_document_stays_document(self):
        from agents.tg_transfer.transfer_engine import TransferEngine
        msg = MagicMock()
        msg.photo = None
        msg.video = None
        msg.document = None
        assert TransferEngine._detect_file_type(msg) == "document"


class TestClassifyPhashDedup:
    """Apply the 3-case decision matrix used by both the album and single
    transfer paths:

      1. all frames match within `per_frame_threshold` AND metadata agrees
         → "auto_skip" (silently drop, mark as skipped — case 1)
      2. some-but-not-all frames match (e.g. 2/3) AND metadata agrees →
         "ambiguous" (block, push into pending_dedup queue for user
         confirmation — case 2)
      3. otherwise → "different" (proceed to upload — case 3)

    Metadata agree = file_size equal AND duration equal (or both None).
    A `None` on either side counts as "unknown" and DOES NOT match — we
    can't auto-skip without confirmation that the metadata lines up.

    Returned tuple is (decision, matched_row, (matched_frames, total_frames))
    so callers can build pending_dedup rows / log diagnostics with the
    matched candidate's identity.
    """

    def _cand(
        self, *, phash, file_size=100, duration=None,
        media_id=1, target_msg_id=10, file_type="video",
    ):
        return {
            "media_id": media_id, "target_msg_id": target_msg_id,
            "phash": phash, "file_size": file_size, "duration": duration,
            "file_type": file_type, "caption": "",
        }

    def test_no_candidates_is_different(self):
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        decision, row, frames = classify_phash_dedup(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10, candidates=[],
            per_frame_threshold=10,
        )
        assert decision == "different"
        assert row is None
        assert frames == (0, 0)

    def test_full_match_with_metadata_match_is_auto_skip(self):
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        cand = self._cand(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10,
        )
        decision, row, frames = classify_phash_dedup(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10, candidates=[cand],
            per_frame_threshold=10,
        )
        assert decision == "auto_skip"
        assert row is cand
        assert frames == (3, 3)

    def test_full_match_metadata_mismatch_is_different(self):
        """User-stated case-3 fallback: 3/3 phash but file_size differs is
        treated as 'different' (upload), not auto-skip — re-encoded videos
        with byte-different output should not silently drop."""
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        cand = self._cand(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=999, duration=10,
        )
        decision, _, _ = classify_phash_dedup(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10, candidates=[cand],
            per_frame_threshold=10,
        )
        assert decision == "different"

    def test_partial_match_with_metadata_match_is_ambiguous(self):
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        # frame 1 differs by 64 bits, frames 0 and 2 identical → 2/3.
        cand = self._cand(
            phash="0000000000000000,eeeeeeeeeeeeeeee,2222222222222222",
            file_size=100, duration=10,
        )
        decision, row, frames = classify_phash_dedup(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10, candidates=[cand],
            per_frame_threshold=10,
        )
        assert decision == "ambiguous"
        assert row is cand
        assert frames == (2, 3)

    def test_partial_match_metadata_mismatch_is_different(self):
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        cand = self._cand(
            phash="0000000000000000,eeeeeeeeeeeeeeee,2222222222222222",
            file_size=999, duration=10,
        )
        decision, _, _ = classify_phash_dedup(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10, candidates=[cand],
            per_frame_threshold=10,
        )
        assert decision == "different"

    def test_zero_match_is_different(self):
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        cand = self._cand(
            phash="ffffffffffffffff,ffffffffffffffff,ffffffffffffffff",
            file_size=100, duration=10,
        )
        decision, _, _ = classify_phash_dedup(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10, candidates=[cand],
            per_frame_threshold=10,
        )
        assert decision == "different"

    def test_single_frame_photo_match_is_auto_skip(self):
        """Photos always have a single-frame phash — the (1, 1) result from
        hamming_distance_multi means 'all frames match' so the auto_skip
        branch fires when metadata matches."""
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        cand = self._cand(
            phash="0000000000000000", file_size=100, duration=None,
            file_type="photo",
        )
        decision, _, frames = classify_phash_dedup(
            phash="0000000000000000", file_size=100, duration=None,
            candidates=[cand], per_frame_threshold=10,
        )
        assert decision == "auto_skip"
        assert frames == (1, 1)

    def test_single_frame_legacy_compared_to_multi_frame(self):
        """Cross-format compare: source has new 3-frame CSV, target row is
        legacy single-frame. hamming_distance_multi reports (1, 1) — only
        comparing frame 0. Metadata match → auto_skip on lower confidence."""
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        legacy_cand = self._cand(
            phash="0000000000000000", file_size=100, duration=10,
        )
        decision, _, frames = classify_phash_dedup(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10, candidates=[legacy_cand],
            per_frame_threshold=10,
        )
        assert decision == "auto_skip"
        assert frames == (1, 1)

    def test_picks_best_candidate_full_match_over_partial(self):
        """If multiple candidates match, prefer the highest-fidelity one so
        the (matched, total) reported reflects the strongest signal."""
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        partial_cand = self._cand(
            phash="0000000000000000,eeeeeeeeeeeeeeee,2222222222222222",
            file_size=100, duration=10, media_id=1,
        )
        full_cand = self._cand(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10, media_id=2,
        )
        decision, row, frames = classify_phash_dedup(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10,
            candidates=[partial_cand, full_cand],
            per_frame_threshold=10,
        )
        assert decision == "auto_skip"
        assert row["media_id"] == 2
        assert frames == (3, 3)

    def test_metadata_none_does_not_match_metadata(self):
        """If either side has duration=None, treat as 'unknown' — don't
        auto-skip on a phash hit alone, surface as ambiguous so user
        confirms."""
        from agents.tg_transfer.transfer_engine import classify_phash_dedup
        cand = self._cand(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=None,  # candidate missing duration
        )
        decision, _, _ = classify_phash_dedup(
            phash="0000000000000000,1111111111111111,2222222222222222",
            file_size=100, duration=10, candidates=[cand],
            per_frame_threshold=10,
        )
        # 3/3 phash but unknown duration → different (let it upload rather
        # than silently drop).
        assert decision == "different"


@pytest.mark.asyncio
async def test_transfer_album_marks_dedup_skipped_in_job_messages(
    engine_with_media_db, mock_client, media_db, db, tmp_path,
):
    """Silent-drop regression: when transfer_album drops a file because of a
    sha256 match, the message must be marked `skipped` in job_messages —
    NOT left in `pending` for run_batch to blanket-mark `success`. The
    bug it guards against: a video silently disappeared from an album
    because a false-positive phash hit dropped it without marking, then
    run_batch's bulk-mark made it look like a successful upload to the
    user."""
    # Pre-populate target with a sha that msg 902 will collide with.
    existing = await media_db.insert_media(
        sha256="dup_sha", phash=None, file_type="photo",
        file_size=10, caption="prior", source_chat="@old",
        source_msg_id=1, target_chat="@dst", job_id="old_job",
    )
    await media_db.mark_uploaded(existing, target_msg_id=77)

    # Create job + job_messages so mark_message UPDATEs hit real rows.
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [901, 902, 903], grouped_ids={
        901: 90, 902: 90, 903: 90,
    })

    target_entity = MagicMock()
    msg1 = _make_message(901, text="cap", grouped_id=90)
    msg2 = _make_message(902, grouped_id=90)
    msg3 = _make_message(903, grouped_id=90)

    def _make_iter(msg, offset=0, **kwargs):
        async def gen():
            yield bytes([msg.id % 256]) * 10
        return gen()
    mock_client.iter_download = _make_iter
    _stub_album_upload(mock_client)

    with patch(
        "agents.tg_transfer.transfer_engine.compute_sha256",
        side_effect=["sha_a", "dup_sha", "sha_c"],
    ):
        with patch(
            "agents.tg_transfer.transfer_engine.compute_phash",
            return_value=None,
        ):
            await engine_with_media_db.transfer_album(
                target_entity, [msg1, msg2, msg3],
                target_chat="@dst", source_chat="@src", job_id=job_id,
            )

    # The dedup-dropped msg 902 must be visible to the caller as skipped,
    # not silently left pending (which would later be overwritten as
    # success by the bulk-mark in run_batch).
    rows = await db.get_grouped_messages(job_id, 90)
    by_id = {r["message_id"]: r["status"] for r in rows}
    assert by_id[902] == "skipped"


@pytest.mark.asyncio
async def test_run_batch_album_preserves_dedup_skipped_status(
    engine_with_media_db, mock_client, media_db, db, tmp_path,
):
    """End-to-end silent-drop fix: run_batch's grouped-message handling
    used to bulk-mark every row in the group with the album's overall
    status, which would overwrite the `skipped` mark transfer_album set
    on a dedup'd message and re-surface it to the user as `success`.
    After the fix run_batch must only mark rows that are still
    `pending`."""
    # Pre-seed dedup hit
    existing = await media_db.insert_media(
        sha256="dup_sha", phash=None, file_type="photo",
        file_size=10, caption=None, source_chat="@old",
        source_msg_id=1, target_chat="@dst", job_id="old_job",
    )
    await media_db.mark_uploaded(existing, target_msg_id=77)

    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [901, 902], grouped_ids={
        901: 90, 902: 90,
    })

    source_entity = MagicMock()
    target_entity = MagicMock()
    msg1 = _make_message(901, text="cap", grouped_id=90)
    msg2 = _make_message(902, grouped_id=90)
    by_msg_id = {901: msg1, 902: msg2}

    async def fake_get_messages(_entity, ids=None):
        return by_msg_id.get(ids)
    mock_client.get_messages = fake_get_messages

    def _make_iter(m, offset=0, **kwargs):
        async def gen():
            yield bytes([m.id % 256]) * 10
        return gen()
    mock_client.iter_download = _make_iter
    _stub_album_upload(mock_client)

    async def _noop(_):
        return None

    with patch(
        "agents.tg_transfer.transfer_engine.compute_sha256",
        side_effect=["sha_a", "dup_sha"],
    ):
        with patch(
            "agents.tg_transfer.transfer_engine.compute_phash",
            return_value=None,
        ):
            await engine_with_media_db.run_batch(
                job_id, source_entity, target_entity, _noop,
            )

    # job_messages rows are deleted when the job hits a terminal status,
    # so observe the bulk-mark vs per-message distinction via the
    # progress snapshot recorded at completion time.
    job = await db.get_job(job_id)
    import json
    snapshot = json.loads(job["final_progress"])
    # 1 successful upload (msg 901), 1 dedup-skip (msg 902). The bug
    # would have shown 2 successes and 0 skipped because run_batch's
    # bulk-mark wiped transfer_album's per-message `skipped`.
    assert snapshot["success"] == 1
    assert snapshot["skipped"] == 1
    assert snapshot["total"] == 2


@pytest.mark.asyncio
async def test_transfer_single_does_not_crash_on_csv_phash_candidate(
    engine_with_media_db, mock_client, media_db, db, tmp_path,
):
    """transfer_single must use the multi-frame classifier when comparing
    against an uploaded video's CSV phash. The legacy code path called
    `hamming_distance(phash, row['phash'])` which raises ValueError on
    `int("h1,h2,h3", 16)` and would crash the per-message dedup
    check."""
    existing = await media_db.insert_media(
        sha256="prior_sha",
        phash="aaaaaaaaaaaaaaaa,bbbbbbbbbbbbbbbb,cccccccccccccccc",
        file_type="video",
        file_size=500, caption=None, source_chat="@old",
        source_msg_id=1, target_chat="@dst", job_id="old_job",
    )
    await media_db.mark_uploaded(existing, target_msg_id=88)

    src = MagicMock()
    target = MagicMock()
    msg = _make_message(1234, text="cap")

    # Use the photo path to avoid mocking ffprobe.
    media_path = str(tmp_path / "downloads" / "1234.jpg")
    os.makedirs(os.path.dirname(media_path), exist_ok=True)
    with open(media_path, "wb") as f:
        f.write(b"PHOTO_BYTES")
    mock_client.download_media = AsyncMock(return_value=media_path)
    mock_client.send_file = AsyncMock(return_value=MagicMock(id=999))

    with patch(
        "agents.tg_transfer.transfer_engine.compute_sha256",
        return_value="new_sha",
    ):
        with patch(
            "agents.tg_transfer.transfer_engine.compute_phash",
            return_value="dddddddddddddddd",
        ):
            # Should not raise ValueError.
            result = await engine_with_media_db.transfer_single(
                src, target, msg,
                target_chat="@dst", source_chat="@src", job_id=None,
            )
    assert result.get("ok") is True or result.get("similar") is not None


@pytest.mark.asyncio
async def test_transfer_album_does_not_crash_on_csv_phash(
    engine_with_media_db, mock_client, media_db, db, tmp_path,
):
    """Phase-after-multi-frame regression: video phashes are now stored as
    CSVs (`"h1,h2,h3"`) but the legacy comparison used
    `hamming_distance(phash, row["phash"])` which calls `int(csv, 16)`
    and raises ValueError on commas. That would crash the entire album
    flow on the first video. Use the multi-frame classifier so CSVs are
    handled correctly."""
    # Seed an uploaded video with a 3-frame CSV phash so the dedup loop
    # hits it as a candidate.
    existing = await media_db.insert_media(
        sha256="prior_video_sha",
        phash="aaaaaaaaaaaaaaaa,bbbbbbbbbbbbbbbb,cccccccccccccccc",
        file_type="video",
        file_size=500, caption=None, source_chat="@old",
        source_msg_id=1, target_chat="@dst", job_id="old_job",
    )
    await media_db.mark_uploaded(existing, target_msg_id=88)

    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [801], grouped_ids={801: 80})

    target_entity = MagicMock()
    msg = _make_message(801, text="cap", grouped_id=80)

    def _make_iter(m, offset=0, **kwargs):
        async def gen():
            yield bytes([m.id % 256]) * 10
        return gen()
    mock_client.iter_download = _make_iter
    _stub_album_upload(mock_client)

    # Photo path so we don't have to mock ffprobe; the candidate is a
    # video CSV phash that the legacy code path would crash on.
    with patch(
        "agents.tg_transfer.transfer_engine.compute_sha256",
        return_value="new_sha",
    ):
        with patch(
            "agents.tg_transfer.transfer_engine.compute_phash",
            return_value="dddddddddddddddd",
        ):
            # Should not raise ValueError despite candidate phash being a CSV.
            ok = await engine_with_media_db.transfer_album(
                target_entity, [msg],
                target_chat="@dst", source_chat="@src", job_id=job_id,
            )
    assert ok is True


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

    captured_paths = []

    class FakeClient:
        async def download_media(self, msg, file):
            captured_paths.append(file)
            with open(file, "wb") as f:
                f.write(b"x" * 16)
            return file

        # Bypass upload — this test only verifies download path construction.
        async def send_file(self, *args, **kwargs):
            return type("M", (), {"id": 1})()

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
    await engine._transfer_media(
        target_entity=None, message=FakeMsg(),
        target_chat="t", source_chat="s",
        job_id=None, skip_pre_dedup=True,
        task_id="task-ABC",
    )

    expected_dir = os.path.join(str(tmp_path / "tmp"), "task-ABC")
    assert os.path.isdir(expected_dir)
    assert captured_paths, "download_media was never called"
    for p in captured_paths:
        assert p.startswith(expected_dir + os.sep), (
            f"{p!r} not under {expected_dir!r}"
        )
    await db.close()


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


@pytest.mark.asyncio
async def test_transfer_album_video_runs_ffprobe_only_once_per_file(tmp_path, monkeypatch):
    """Album with N videos must call ffprobe_metadata exactly N times,
    not 2N — meta is computed once per file in the dedup loop and
    reused in the upload-prep loop."""
    db = TransferDB(str(tmp_path / "t.db"))
    await db.init()

    class FakeClient:
        async def download_media(self, msg, file):
            with open(file, "wb") as f:
                f.write(b"x" * 16)
            return file

    engine = TransferEngine(
        client=FakeClient(), db=db, tmp_dir=str(tmp_path / "tmp"),
    )

    class FakeMsg:
        def __init__(self, mid):
            self.id = mid
            self.text = ""
            self.media = object()
            self.file = type("F", (), {"size": 16, "name": None, "ext": ".mp4"})()

    monkeypatch.setattr(engine, "should_skip", lambda _m: False)
    monkeypatch.setattr(engine, "_detect_file_type", lambda _m: "video")

    ffprobe_calls = []
    async def fake_ffprobe(path):
        ffprobe_calls.append(path)
        return {"duration": 10, "width": 100, "height": 50}
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

    # Bypass dedup classification so the dedup loop completes without
    # short-circuiting and per_file_meta is fully populated.
    monkeypatch.setattr(
        "agents.tg_transfer.transfer_engine.classify_phash_dedup",
        lambda **_kw: ("upload", None, (0, 0)),
    )

    # Bypass actual upload — we only care about ffprobe count
    async def _noop_upload(*args, **kwargs):
        return [type("M", (), {"id": 1})()]
    monkeypatch.setattr(engine, "_upload_album_manual", _noop_upload)

    msgs = [FakeMsg(101), FakeMsg(102), FakeMsg(103)]
    ok = await engine.transfer_album(
        target_entity=None, messages=msgs,
        target_chat="t", source_chat="s", task_id="task-ALB",
    )
    assert ok is True

    # 3 videos × 1 ffprobe each = 3 (not 6)
    assert len(ffprobe_calls) == 3, (
        f"expected 3 ffprobe calls (1 per video), got {len(ffprobe_calls)}"
    )
    await db.close()


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
        async def upsert_pending(self, *a, **k):
            return 1

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
