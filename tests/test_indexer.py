"""Tests for the target-chat indexer (Phase 2 of cross-source dedup)."""
import io
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from PIL import Image

from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.media_db import MediaDB


def _png_bytes(color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color=color).save(buf, format="PNG")
    return buf.getvalue()


def _make_msg(msg_id, media_kind="photo", caption=None, duration=None,
              file_size=None, grouped_id=None):
    """Build a Telethon-shaped message mock with just the fields the
    indexer reads."""
    m = MagicMock()
    m.id = msg_id
    m.message = caption
    m.text = caption
    m.grouped_id = grouped_id
    m.media = MagicMock() if media_kind else None
    if media_kind == "photo":
        m.photo = MagicMock()
        m.video = None
        m.document = None
        m.voice = None
        m.sticker = None
        m.file = MagicMock(size=file_size, duration=None)
    elif media_kind == "video":
        m.photo = None
        m.video = MagicMock()
        m.document = None
        m.voice = None
        m.sticker = None
        m.file = MagicMock(size=file_size, duration=duration)
    else:
        m.photo = None
        m.video = None
        m.document = None
        m.voice = None
        m.sticker = None
        m.media = None
        m.file = None
    return m


@pytest_asyncio.fixture
async def dbs(tmp_path):
    tdb = TransferDB(str(tmp_path / "t.db"))
    mdb = MediaDB(str(tmp_path / "m.db"))
    await tdb.init()
    await mdb.init()
    yield tdb, mdb
    await tdb.close()
    await mdb.close()


@pytest.fixture
def fake_client():
    """Telethon-like client whose iter_messages yields a fixed list."""
    c = AsyncMock()
    c._messages = []  # test will set

    async def _iter(entity, min_id=0, reverse=True, **kwargs):
        for msg in c._messages:
            if msg.id > min_id:
                yield msg

    c.iter_messages = _iter
    c.download_media = AsyncMock(return_value=_png_bytes("red"))
    c.get_entity = AsyncMock(return_value=MagicMock())
    return c


class TestTargetIndexer:
    @pytest.mark.asyncio
    async def test_scan_inserts_thumb_only_row_for_each_media_message(
        self, dbs, fake_client,
    ):
        from agents.tg_transfer.indexer import TargetIndexer
        tdb, mdb = dbs
        fake_client._messages = [
            _make_msg(10, "photo", caption="sunset", file_size=1000),
            _make_msg(11, "video", caption="clip",
                      duration=42, file_size=50_000),
        ]
        idx = TargetIndexer(client=fake_client, tdb=tdb, mdb=mdb)

        stats = await idx.scan_target("@target")

        assert stats["scanned"] == 2
        assert stats["inserted"] == 2

        async with mdb._db.execute(
            "SELECT target_msg_id, trust, file_type, caption, duration "
            "FROM media WHERE target_chat = '@target' "
            "ORDER BY target_msg_id"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        assert [r["target_msg_id"] for r in rows] == [10, 11]
        assert all(r["trust"] == "thumb_only" for r in rows)
        assert rows[1]["duration"] == 42

    @pytest.mark.asyncio
    async def test_scan_skips_text_only_messages(self, dbs, fake_client):
        from agents.tg_transfer.indexer import TargetIndexer
        tdb, mdb = dbs
        fake_client._messages = [
            _make_msg(1, media_kind=None, caption="just text"),
            _make_msg(2, "photo", caption="img", file_size=100),
        ]
        idx = TargetIndexer(client=fake_client, tdb=tdb, mdb=mdb)
        stats = await idx.scan_target("@target")
        assert stats["scanned"] == 2
        assert stats["inserted"] == 1

        async with mdb._db.execute(
            "SELECT COUNT(*) as c FROM media WHERE target_chat = '@target'"
        ) as cur:
            assert (await cur.fetchone())["c"] == 1

    @pytest.mark.asyncio
    async def test_scan_records_last_scanned_for_resume(
        self, dbs, fake_client,
    ):
        """After scan, the highest msg_id must be stored so the next scan
        picks up from there (incremental mode)."""
        from agents.tg_transfer.indexer import TargetIndexer
        tdb, mdb = dbs
        fake_client._messages = [
            _make_msg(5, "photo", caption="a", file_size=10),
            _make_msg(9, "photo", caption="b", file_size=20),
        ]
        idx = TargetIndexer(client=fake_client, tdb=tdb, mdb=mdb)
        await idx.scan_target("@target")

        stored = await tdb.get_config("last_scanned_msg_id_@target")
        assert stored == "9"

    @pytest.mark.asyncio
    async def test_scan_resumes_from_last_scanned(self, dbs, fake_client):
        """If last_scanned_msg_id is N, iter_messages must start at N+1
        (we pass min_id=N). Already-scanned rows are not re-processed."""
        from agents.tg_transfer.indexer import TargetIndexer
        tdb, mdb = dbs
        await tdb.set_config("last_scanned_msg_id_@target", "100")

        fake_client._messages = [
            _make_msg(100, "photo", caption="old", file_size=1),
            _make_msg(101, "photo", caption="new", file_size=2),
        ]
        idx = TargetIndexer(client=fake_client, tdb=tdb, mdb=mdb)
        stats = await idx.scan_target("@target")

        # msg 100 filtered out by min_id=100
        assert stats["scanned"] == 1
        assert stats["inserted"] == 1
        stored = await tdb.get_config("last_scanned_msg_id_@target")
        assert stored == "101"

    @pytest.mark.asyncio
    async def test_scan_is_idempotent_on_rerun(self, dbs, fake_client):
        """Running scan twice on same messages shouldn't duplicate rows."""
        from agents.tg_transfer.indexer import TargetIndexer
        tdb, mdb = dbs
        fake_client._messages = [
            _make_msg(50, "photo", caption="x", file_size=10),
        ]
        idx = TargetIndexer(client=fake_client, tdb=tdb, mdb=mdb)
        await idx.scan_target("@target")
        # Drop last_scanned so second run re-processes the same msg.
        await tdb.set_config("last_scanned_msg_id_@target", "0")
        await idx.scan_target("@target")

        async with mdb._db.execute(
            "SELECT COUNT(*) as c FROM media WHERE target_chat = '@target'"
        ) as cur:
            assert (await cur.fetchone())["c"] == 1

    @pytest.mark.asyncio
    async def test_scan_fires_progress_callback_every_10pct_when_over_1000(
        self, dbs, fake_client,
    ):
        """total > 1000 → callback receives (scanned, total) every 10%.
        Callback must receive 10 updates (10%..100%)."""
        from agents.tg_transfer.indexer import TargetIndexer
        tdb, mdb = dbs
        fake_client._messages = [
            _make_msg(i, "photo", caption="x", file_size=1)
            for i in range(1, 1201)
        ]
        idx = TargetIndexer(client=fake_client, tdb=tdb, mdb=mdb)

        calls = []

        async def on_progress(scanned, total):
            calls.append((scanned, total))

        await idx.scan_target(
            "@target", total_hint=1200, progress_cb=on_progress,
        )

        # 10% steps over 1200 → every 120 messages → 10 fires total.
        assert len(calls) == 10
        assert calls[0][1] == 1200
        assert calls[-1][0] == 1200

    @pytest.mark.asyncio
    async def test_scan_does_not_fire_progress_when_under_1000(
        self, dbs, fake_client,
    ):
        from agents.tg_transfer.indexer import TargetIndexer
        tdb, mdb = dbs
        fake_client._messages = [
            _make_msg(i, "photo", caption="x", file_size=1)
            for i in range(1, 51)
        ]
        idx = TargetIndexer(client=fake_client, tdb=tdb, mdb=mdb)

        calls = []

        async def on_progress(scanned, total):
            calls.append((scanned, total))

        await idx.scan_target(
            "@target", total_hint=50, progress_cb=on_progress,
        )
        assert calls == []
