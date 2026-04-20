import pytest
import pytest_asyncio
from agents.tg_transfer.media_db import MediaDB


@pytest_asyncio.fixture
async def mdb(tmp_path):
    db = MediaDB(str(tmp_path / "media_test.db"))
    await db.init()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_insert_and_get_media(mdb):
    media_id = await mdb.insert_media(
        sha256="abc123", phash="1234567890abcdef", file_type="photo",
        file_size=1024, caption="test #tag1", source_chat="@src",
        source_msg_id=100, target_chat="@dst", job_id="job1",
    )
    media = await mdb.get_media(media_id)
    assert media["sha256"] == "abc123"
    assert media["status"] == "pending"
    assert media["target_msg_id"] is None


@pytest.mark.asyncio
async def test_mark_uploaded(mdb):
    media_id = await mdb.insert_media(
        sha256="aaa", phash=None, file_type="document",
        file_size=500, caption=None, source_chat="@s",
        source_msg_id=1, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(media_id, target_msg_id=999)
    media = await mdb.get_media(media_id)
    assert media["status"] == "uploaded"
    assert media["target_msg_id"] == 999


@pytest.mark.asyncio
async def test_mark_skipped(mdb):
    media_id = await mdb.insert_media(
        sha256="bbb", phash=None, file_type="video",
        file_size=2000, caption=None, source_chat="@s",
        source_msg_id=2, target_chat="@d", job_id="j1",
    )
    await mdb.mark_skipped(media_id)
    media = await mdb.get_media(media_id)
    assert media["status"] == "skipped"


@pytest.mark.asyncio
async def test_delete_media(mdb):
    media_id = await mdb.insert_media(
        sha256="ccc", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=3, target_chat="@d", job_id="j1",
    )
    await mdb.delete_media(media_id)
    assert await mdb.get_media(media_id) is None


@pytest.mark.asyncio
async def test_find_by_sha256(mdb):
    mid = await mdb.insert_media(
        sha256="dup", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=10, target_chat="@dst", job_id="j1",
    )
    await mdb.mark_uploaded(mid, target_msg_id=50)
    found = await mdb.find_by_sha256("dup", "@dst")
    assert found is not None
    assert found["status"] == "uploaded"


@pytest.mark.asyncio
async def test_find_by_sha256_not_found(mdb):
    found = await mdb.find_by_sha256("nonexistent", "@dst")
    assert found is None


@pytest.mark.asyncio
async def test_find_by_sha256_ignores_non_uploaded(mdb):
    """Only 'uploaded' rows block re-upload. pending/failed/skipped rows mean
    the media is 待上傳 (awaiting upload) and should not trigger dedup."""
    mid = await mdb.insert_media(
        sha256="retry", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=11, target_chat="@dst", job_id="j1",
    )
    # Row exists as 'pending' (default)
    assert await mdb.find_by_sha256("retry", "@dst") is None

    # 'failed' also doesn't trigger dedup
    await mdb.mark_failed(mid)
    assert await mdb.find_by_sha256("retry", "@dst") is None

    # 'skipped' also doesn't trigger dedup
    await mdb.mark_skipped(mid)
    assert await mdb.find_by_sha256("retry", "@dst") is None

    # Only 'uploaded' does
    await mdb.mark_uploaded(mid, target_msg_id=55)
    assert await mdb.find_by_sha256("retry", "@dst") is not None


@pytest.mark.asyncio
async def test_mark_failed(mdb):
    mid = await mdb.insert_media(
        sha256="fail1", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=12, target_chat="@d", job_id="j1",
    )
    await mdb.mark_failed(mid)
    media = await mdb.get_media(mid)
    assert media["status"] == "failed"


@pytest.mark.asyncio
async def test_upsert_pending_inserts_when_missing(mdb):
    """New (sha256, target_chat) → insert a fresh pending row."""
    mid = await mdb.upsert_pending(
        sha256="new1", phash=None, file_type="photo",
        file_size=100, caption="caption1", source_chat="@s",
        source_msg_id=20, target_chat="@d", job_id="j1",
    )
    assert mid is not None
    media = await mdb.get_media(mid)
    assert media["status"] == "pending"
    assert media["caption"] == "caption1"


@pytest.mark.asyncio
async def test_upsert_pending_revives_failed_row(mdb):
    """Existing non-uploaded row (failed/skipped/pending) → update it back to
    pending and reuse the same media_id so retry continues from same record."""
    first = await mdb.insert_media(
        sha256="revive", phash=None, file_type="photo",
        file_size=100, caption="old", source_chat="@s",
        source_msg_id=30, target_chat="@d", job_id="j1",
    )
    await mdb.mark_failed(first)

    second = await mdb.upsert_pending(
        sha256="revive", phash="newphash", file_type="photo",
        file_size=200, caption="new", source_chat="@s",
        source_msg_id=31, target_chat="@d", job_id="j2",
    )
    assert second == first, "should reuse the same media_id"
    media = await mdb.get_media(first)
    assert media["status"] == "pending"
    assert media["caption"] == "new"
    assert media["file_size"] == 200
    assert media["phash"] == "newphash"
    assert media["source_msg_id"] == 31
    assert media["job_id"] == "j2"


@pytest.mark.asyncio
async def test_upsert_pending_rejects_uploaded_row(mdb):
    """If an 'uploaded' row already exists, upsert_pending must NOT overwrite
    it. Returns None so caller can branch to dedup path."""
    first = await mdb.insert_media(
        sha256="uploaded_guard", phash=None, file_type="photo",
        file_size=100, caption="kept", source_chat="@s",
        source_msg_id=40, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(first, target_msg_id=555)

    result = await mdb.upsert_pending(
        sha256="uploaded_guard", phash="x", file_type="photo",
        file_size=999, caption="should not overwrite", source_chat="@s",
        source_msg_id=41, target_chat="@d", job_id="j2",
    )
    assert result is None, "must refuse to overwrite uploaded rows"
    media = await mdb.get_media(first)
    assert media["status"] == "uploaded"
    assert media["caption"] == "kept"
    assert media["file_size"] == 100


@pytest.mark.asyncio
async def test_find_similar_phash(mdb):
    mid = await mdb.insert_media(
        sha256="x1", phash="0000000000000000", file_type="photo",
        file_size=100, caption="similar photo", source_chat="@s",
        source_msg_id=20, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(mid, target_msg_id=60)
    results = await mdb.get_all_phashes()
    assert len(results) == 1
    assert results[0]["phash"] == "0000000000000000"


@pytest.mark.asyncio
async def test_tags_crud(mdb):
    media_id = await mdb.insert_media(
        sha256="t1", phash=None, file_type="photo",
        file_size=100, caption="#教學 #python", source_chat="@s",
        source_msg_id=30, target_chat="@d", job_id="j1",
    )
    await mdb.add_tags(media_id, ["教學", "python"])
    tags = await mdb.get_tags(media_id)
    assert set(tags) == {"教學", "python"}


@pytest.mark.asyncio
async def test_search_by_keyword(mdb):
    m1 = await mdb.insert_media(
        sha256="s1", phash=None, file_type="photo",
        file_size=100, caption="Python 教學影片", source_chat="@s",
        source_msg_id=40, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(m1, target_msg_id=70)
    m2 = await mdb.insert_media(
        sha256="s2", phash=None, file_type="video",
        file_size=200, caption="Rust 教學", source_chat="@s",
        source_msg_id=41, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(m2, target_msg_id=71)
    results, total = await mdb.search_keyword("教學", page=1, page_size=10)
    assert total == 2
    assert len(results) == 2


@pytest.mark.asyncio
async def test_search_by_tag(mdb):
    m1 = await mdb.insert_media(
        sha256="st1", phash=None, file_type="photo",
        file_size=100, caption="#mytag something", source_chat="@s",
        source_msg_id=50, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(m1, target_msg_id=80)
    await mdb.add_tags(m1, ["mytag"])
    results, total = await mdb.search_keyword("mytag", page=1, page_size=10)
    assert total >= 1


@pytest.mark.asyncio
async def test_search_pagination(mdb):
    for i in range(15):
        mid = await mdb.insert_media(
            sha256=f"pg{i}", phash=None, file_type="photo",
            file_size=100, caption=f"page test item {i}", source_chat="@s",
            source_msg_id=100 + i, target_chat="@d", job_id="j1",
        )
        await mdb.mark_uploaded(mid, target_msg_id=200 + i)
    page1, total = await mdb.search_keyword("page test", page=1, page_size=10)
    assert total == 15
    assert len(page1) == 10
    page2, _ = await mdb.search_keyword("page test", page=2, page_size=10)
    assert len(page2) == 5


@pytest.mark.asyncio
async def test_get_stats(mdb):
    m1 = await mdb.insert_media(
        sha256="stat1", phash=None, file_type="photo",
        file_size=100, caption="#a #b", source_chat="@s",
        source_msg_id=60, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(m1, target_msg_id=90)
    await mdb.add_tags(m1, ["a", "b"])
    m2 = await mdb.insert_media(
        sha256="stat2", phash=None, file_type="video",
        file_size=200, caption="#a", source_chat="@s",
        source_msg_id=61, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(m2, target_msg_id=91)
    await mdb.add_tags(m2, ["a"])
    stats = await mdb.get_stats()
    assert stats["total_media"] == 2
    assert stats["total_tags"] == 2
    assert stats["tag_counts"][0] == ("a", 2)
    assert stats["tag_counts"][1] == ("b", 1)


@pytest.mark.asyncio
async def test_get_stats_groups_by_status(mdb):
    """Dashboard needs per-status breakdown so user sees how many items are
    still pending/failed/skipped besides uploaded."""
    # uploaded x2
    for i, sm in enumerate([300, 301]):
        mid = await mdb.insert_media(
            sha256=f"up{i}", phash=None, file_type="photo",
            file_size=100, caption=None, source_chat="@s",
            source_msg_id=sm, target_chat="@d", job_id="j",
        )
        await mdb.mark_uploaded(mid, target_msg_id=900 + i)
    # failed x1
    mid_f = await mdb.insert_media(
        sha256="fa1", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=310, target_chat="@d", job_id="j",
    )
    await mdb.mark_failed(mid_f)
    # skipped x1
    mid_s = await mdb.insert_media(
        sha256="sk1", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=320, target_chat="@d", job_id="j",
    )
    await mdb.mark_skipped(mid_s)
    # pending x1 (default after insert)
    await mdb.insert_media(
        sha256="pd1", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=330, target_chat="@d", job_id="j",
    )

    stats = await mdb.get_stats()
    # Backward compat: total_media still = uploaded count
    assert stats["total_media"] == 2
    # New: by_status breakdown
    assert stats["by_status"]["uploaded"] == 2
    assert stats["by_status"]["failed"] == 1
    assert stats["by_status"]["skipped"] == 1
    assert stats["by_status"]["pending"] == 1


@pytest.mark.asyncio
async def test_get_stale_media(mdb):
    m1 = await mdb.insert_media(
        sha256="stale1", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=70, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(m1, target_msg_id=100)
    # Force last_checked_at to old value
    await mdb._db.execute(
        "UPDATE media SET last_checked_at = datetime('now', '-48 hours') WHERE media_id = ?",
        (m1,),
    )
    await mdb._db.commit()
    stale = await mdb.get_stale_media(max_age_hours=24, limit=50)
    assert len(stale) == 1
    assert stale[0]["media_id"] == m1


@pytest.mark.asyncio
async def test_update_last_checked(mdb):
    m1 = await mdb.insert_media(
        sha256="chk1", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=80, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(m1, target_msg_id=110)
    await mdb.update_last_checked(m1)
    media = await mdb.get_media(m1)
    assert media["last_checked_at"] is not None
