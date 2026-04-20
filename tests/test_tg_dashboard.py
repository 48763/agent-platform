import pytest
import pytest_asyncio
from aiohttp.test_utils import make_mocked_request

from agents.tg_transfer.dashboard import create_tg_dashboard_handler
from agents.tg_transfer.media_db import MediaDB


@pytest_asyncio.fixture
async def mdb(tmp_path):
    db = MediaDB(str(tmp_path / "dashboard.db"))
    await db.init()
    yield db
    await db.close()


class TestTgDashboardCounters:
    """Dashboard should surface media counts by file type as top-level
    counters instead of a bulky status table."""

    @pytest.mark.asyncio
    async def test_counters_include_each_known_type(self, mdb):
        p = await mdb.insert_media(
            sha256="p1", phash=None, file_type="photo", file_size=1,
            caption=None, source_chat="@s", source_msg_id=1, target_chat="@d1",
        )
        v = await mdb.insert_media(
            sha256="v1", phash=None, file_type="video", file_size=1,
            caption=None, source_chat="@s", source_msg_id=2, target_chat="@d2",
        )
        await mdb.mark_uploaded(p, target_msg_id=10)
        await mdb.mark_uploaded(v, target_msg_id=20)

        handler = create_tg_dashboard_handler(mdb)
        # Call the underlying get_stats by invoking the handler with a
        # request and inspecting the HTML. We just need to make sure the
        # numbers are in the output.
        req = make_mocked_request("GET", "/dashboard")
        resp = await handler(req)
        body = resp.body.decode("utf-8")

        # Per-type counters rendered
        assert "📷 圖片" in body
        assert "🎬 影片" in body
        # The old bulky status table should no longer be there
        assert "媒體狀態" not in body
        # Values present
        assert ">1<" in body or ">1 " in body or " 1\n" in body or "1</" in body

    @pytest.mark.asyncio
    async def test_uninitialized_media_db(self):
        handler = create_tg_dashboard_handler(None)
        req = make_mocked_request("GET", "/dashboard")
        resp = await handler(req)
        assert resp.status == 200
        assert "未初始化" in resp.body.decode("utf-8")
