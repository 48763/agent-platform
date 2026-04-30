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


class TestGetAllPhashesFilters:
    """`get_all_phashes` must let the dedup path scope the candidate
    set so a video's phash can't accidentally match an image (and vice
    versa) and so a target's index doesn't bleed into another target's
    dedup decisions. Both filters are optional for backward compat."""

    @pytest.mark.asyncio
    async def test_filter_by_file_type(self, mdb):
        m_photo = await mdb.insert_media(
            sha256="p1", phash="0000000000000000", file_type="photo",
            file_size=100, caption="", source_chat="@s",
            source_msg_id=1, target_chat="@d", job_id="j",
        )
        await mdb.mark_uploaded(m_photo, target_msg_id=1)
        m_video = await mdb.insert_media(
            sha256="v1", phash="1111111111111111", file_type="video",
            file_size=500, caption="", source_chat="@s",
            source_msg_id=2, target_chat="@d", job_id="j",
        )
        await mdb.mark_uploaded(m_video, target_msg_id=2)

        videos = await mdb.get_all_phashes(file_type="video")
        assert [r["phash"] for r in videos] == ["1111111111111111"]

        photos = await mdb.get_all_phashes(file_type="photo")
        assert [r["phash"] for r in photos] == ["0000000000000000"]

    @pytest.mark.asyncio
    async def test_filter_by_target_chat(self, mdb):
        m_a = await mdb.insert_media(
            sha256="a1", phash="aaaaaaaaaaaaaaaa", file_type="photo",
            file_size=100, caption="", source_chat="@s",
            source_msg_id=10, target_chat="@target_a", job_id="j",
        )
        await mdb.mark_uploaded(m_a, target_msg_id=100)
        m_b = await mdb.insert_media(
            sha256="b1", phash="bbbbbbbbbbbbbbbb", file_type="photo",
            file_size=100, caption="", source_chat="@s",
            source_msg_id=11, target_chat="@target_b", job_id="j",
        )
        await mdb.mark_uploaded(m_b, target_msg_id=200)

        only_a = await mdb.get_all_phashes(target_chat="@target_a")
        assert [r["phash"] for r in only_a] == ["aaaaaaaaaaaaaaaa"]

    @pytest.mark.asyncio
    async def test_combined_filters(self, mdb):
        # Same target, different file_types — combined filter narrows further.
        m_v = await mdb.insert_media(
            sha256="cv", phash="cccccccccccccccc", file_type="video",
            file_size=500, caption="", source_chat="@s",
            source_msg_id=20, target_chat="@d", job_id="j",
        )
        await mdb.mark_uploaded(m_v, target_msg_id=20)
        m_p = await mdb.insert_media(
            sha256="cp", phash="dddddddddddddddd", file_type="photo",
            file_size=100, caption="", source_chat="@s",
            source_msg_id=21, target_chat="@d", job_id="j",
        )
        await mdb.mark_uploaded(m_p, target_msg_id=21)

        rows = await mdb.get_all_phashes(file_type="video", target_chat="@d")
        assert [r["phash"] for r in rows] == ["cccccccccccccccc"]

    @pytest.mark.asyncio
    async def test_no_args_keeps_legacy_behaviour(self, mdb):
        """Existing callers that pass no kwargs must still see all rows
        — backward-compat for tests / search / scan code paths."""
        for i, ft in enumerate(["photo", "video", "photo"]):
            mid = await mdb.insert_media(
                sha256=f"any{i}", phash=f"{i:016x}", file_type=ft,
                file_size=10, caption="", source_chat="@s",
                source_msg_id=30 + i, target_chat=f"@t{i % 2}", job_id="j",
            )
            await mdb.mark_uploaded(mid, target_msg_id=30 + i)
        rows = await mdb.get_all_phashes()
        assert len(rows) == 3


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


class TestStatsByType:
    """get_stats() should break uploaded media down by file_type so the
    dashboard can show photos / videos / documents separately."""

    @pytest.mark.asyncio
    async def test_by_type_counts_only_uploaded(self, mdb):
        # Two uploaded photos
        for i, sha in enumerate(["p1", "p2"]):
            mid = await mdb.insert_media(
                sha256=sha, phash=None, file_type="photo", file_size=10,
                caption=None, source_chat="@s", source_msg_id=i,
                target_chat=f"@d{i}",
            )
            await mdb.mark_uploaded(mid, target_msg_id=100 + i)
        # One uploaded video
        v = await mdb.insert_media(
            sha256="v1", phash=None, file_type="video", file_size=100,
            caption=None, source_chat="@s", source_msg_id=50, target_chat="@d",
        )
        await mdb.mark_uploaded(v, target_msg_id=200)
        # One pending photo — should NOT be counted
        await mdb.insert_media(
            sha256="p3", phash=None, file_type="photo", file_size=10,
            caption=None, source_chat="@s", source_msg_id=60, target_chat="@d3",
        )
        stats = await mdb.get_stats()
        assert stats["by_type"] == {"photo": 2, "video": 1}
        # total_media remains the overall uploaded count
        assert stats["total_media"] == 3

    @pytest.mark.asyncio
    async def test_by_type_empty_when_none_uploaded(self, mdb):
        stats = await mdb.get_stats()
        assert stats["by_type"] == {}


class TestTagsUsedOnly:
    """total_tags should count only tags that are still linked to uploaded
    media. Orphan tags (left behind after media is deleted) must not inflate
    the count."""

    @pytest.mark.asyncio
    async def test_orphan_tags_not_counted(self, mdb):
        m = await mdb.insert_media(
            sha256="a", phash=None, file_type="photo", file_size=1,
            caption=None, source_chat="@s", source_msg_id=1, target_chat="@d",
        )
        await mdb.mark_uploaded(m, target_msg_id=10)
        await mdb.add_tags(m, ["used_tag"])

        # Create an orphan tag by inserting directly (simulates leftover from
        # deleted media before we had cascade).
        await mdb._db.execute(
            "INSERT INTO tags (name) VALUES ('orphan_tag')"
        )
        await mdb._db.commit()

        stats = await mdb.get_stats()
        assert stats["total_tags"] == 1  # only 'used_tag'

    @pytest.mark.asyncio
    async def test_tags_only_from_uploaded_media(self, mdb):
        """A tag only linked to a pending/failed/skipped media counts as
        orphan — we only track usage on uploaded."""
        m = await mdb.insert_media(
            sha256="x", phash=None, file_type="photo", file_size=1,
            caption=None, source_chat="@s", source_msg_id=1, target_chat="@d",
        )
        # still pending (not uploaded)
        await mdb.add_tags(m, ["ghost"])
        stats = await mdb.get_stats()
        assert stats["total_tags"] == 0


class TestDeleteMediaCleansTags:
    """Deleting a media should also drop any tags that are left with no
    references (orphan cleanup)."""

    @pytest.mark.asyncio
    async def test_delete_media_removes_orphan_tags(self, mdb):
        m = await mdb.insert_media(
            sha256="a", phash=None, file_type="photo", file_size=1,
            caption=None, source_chat="@s", source_msg_id=1, target_chat="@d",
        )
        await mdb.mark_uploaded(m, target_msg_id=10)
        await mdb.add_tags(m, ["t1", "t2"])

        await mdb.delete_media(m)

        async with mdb._db.execute("SELECT COUNT(*) AS n FROM tags") as cur:
            assert (await cur.fetchone())["n"] == 0

    @pytest.mark.asyncio
    async def test_delete_media_keeps_tags_still_used(self, mdb):
        m1 = await mdb.insert_media(
            sha256="a", phash=None, file_type="photo", file_size=1,
            caption=None, source_chat="@s", source_msg_id=1, target_chat="@d1",
        )
        m2 = await mdb.insert_media(
            sha256="b", phash=None, file_type="photo", file_size=1,
            caption=None, source_chat="@s", source_msg_id=2, target_chat="@d2",
        )
        await mdb.mark_uploaded(m1, target_msg_id=10)
        await mdb.mark_uploaded(m2, target_msg_id=20)
        await mdb.add_tags(m1, ["shared", "only_on_1"])
        await mdb.add_tags(m2, ["shared"])

        await mdb.delete_media(m1)

        async with mdb._db.execute(
            "SELECT name FROM tags ORDER BY name"
        ) as cur:
            names = [r["name"] for r in await cur.fetchall()]
        assert names == ["shared"]


class TestNewSchemaColumns:
    """Phase 1 of cross-source dedup: three-level hash trust model.
    The media schema gains thumb_phash / duration / trust / verified_by
    so we can index target chats from TG thumbnails alone (no full
    download). sha256 / phash become nullable because scanned rows don't
    have them."""

    @pytest.mark.asyncio
    async def test_sha256_is_nullable(self, mdb):
        """Scanned rows have no full-file hash. Schema must allow it."""
        await mdb._db.execute(
            "INSERT INTO media (sha256, phash, file_type, source_chat, "
            "source_msg_id, target_chat, target_msg_id, status) "
            "VALUES (NULL, NULL, 'photo', '@s', 1, '@t', 10, 'uploaded')"
        )
        await mdb._db.commit()
        async with mdb._db.execute(
            "SELECT sha256 FROM media WHERE target_msg_id = 10"
        ) as cur:
            row = await cur.fetchone()
        assert row["sha256"] is None

    @pytest.mark.asyncio
    async def test_thumb_phash_column_exists(self, mdb):
        async with mdb._db.execute("PRAGMA table_info(media)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "thumb_phash" in cols

    @pytest.mark.asyncio
    async def test_duration_column_exists(self, mdb):
        async with mdb._db.execute("PRAGMA table_info(media)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "duration" in cols

    @pytest.mark.asyncio
    async def test_trust_column_defaults_to_full(self, mdb):
        mid = await mdb.insert_media(
            sha256="tr1", phash=None, file_type="photo", file_size=1,
            caption=None, source_chat="@s", source_msg_id=1, target_chat="@t",
        )
        media = await mdb.get_media(mid)
        # Transfer path (has sha256) → trust full by default.
        assert media["trust"] == "full"

    @pytest.mark.asyncio
    async def test_verified_by_column_exists_and_nullable(self, mdb):
        mid = await mdb.insert_media(
            sha256="vb1", phash=None, file_type="photo", file_size=1,
            caption=None, source_chat="@s", source_msg_id=1, target_chat="@t",
        )
        media = await mdb.get_media(mid)
        assert "verified_by" in media
        assert media["verified_by"] is None

    @pytest.mark.asyncio
    async def test_thumb_phash_index_exists(self, mdb):
        async with mdb._db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='media'"
        ) as cur:
            names = {r["name"] for r in await cur.fetchall()}
        # Either named explicitly or any index whose SQL references thumb_phash
        async with mdb._db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='media'"
        ) as cur:
            sqls = [r["sql"] or "" for r in await cur.fetchall()]
        assert any("thumb_phash" in s for s in sqls), (
            f"No index on thumb_phash; indexes: {names}"
        )


class TestMediaDBMigration:
    """Legacy DBs (sha256 NOT NULL, no new columns) must be upgraded
    in-place without losing existing rows."""

    @pytest.mark.asyncio
    async def test_legacy_db_gets_new_columns_preserving_rows(self, tmp_path):
        import aiosqlite
        path = str(tmp_path / "legacy_media.db")
        legacy = await aiosqlite.connect(path)
        await legacy.executescript("""
            CREATE TABLE media (
                media_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL,
                phash TEXT,
                file_type TEXT NOT NULL,
                file_size INTEGER,
                caption TEXT,
                source_chat TEXT NOT NULL,
                source_msg_id INTEGER NOT NULL,
                target_chat TEXT NOT NULL,
                target_msg_id INTEGER,
                status TEXT DEFAULT 'pending',
                job_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TIMESTAMP
            );
            CREATE UNIQUE INDEX idx_media_sha256_target ON media(sha256, target_chat);
            CREATE TABLE tags (
                tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE media_tags (
                media_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (media_id, tag_id)
            );
            INSERT INTO media (sha256, file_type, source_chat, source_msg_id,
                               target_chat, status)
            VALUES ('legacy_sha', 'photo', '@s', 99, '@t', 'uploaded');
        """)
        await legacy.commit()
        await legacy.close()

        db = MediaDB(path)
        await db.init()
        try:
            # New columns present
            async with db._db.execute("PRAGMA table_info(media)") as cur:
                cols = {row["name"] for row in await cur.fetchall()}
            assert {"thumb_phash", "duration", "trust", "verified_by"} <= cols

            # Old row preserved
            async with db._db.execute(
                "SELECT * FROM media WHERE sha256 = 'legacy_sha'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row["target_chat"] == "@t"
            assert row["trust"] == "full"  # legacy rows default to full

            # After migration, can insert a scanned row with NULL sha256
            await db._db.execute(
                "INSERT INTO media (sha256, file_type, source_chat, "
                "source_msg_id, target_chat, target_msg_id, status, "
                "thumb_phash, trust) "
                "VALUES (NULL, 'photo', '@s', 100, '@t', 500, 'uploaded', "
                "'abcd1234', 'thumb_only')"
            )
            await db._db.commit()
            async with db._db.execute(
                "SELECT trust, thumb_phash FROM media WHERE target_msg_id = 500"
            ) as cur:
                scanned = await cur.fetchone()
            assert scanned["trust"] == "thumb_only"
            assert scanned["thumb_phash"] == "abcd1234"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_migration_is_idempotent(self, tmp_path):
        """Running init() twice on the same DB shouldn't error or duplicate."""
        path = str(tmp_path / "idem.db")
        db1 = MediaDB(path)
        await db1.init()
        mid = await db1.insert_media(
            sha256="x", phash=None, file_type="photo", file_size=1,
            caption=None, source_chat="@s", source_msg_id=1, target_chat="@t",
        )
        await db1.close()

        db2 = MediaDB(path)
        await db2.init()
        try:
            media = await db2.get_media(mid)
            assert media is not None
            assert media["sha256"] == "x"
        finally:
            await db2.close()


class TestInsertThumbRecord:
    """Phase 2 — /index_target scan produces thumb-only rows. These rows
    represent media that exists in the target chat but whose full-file
    hashes (sha256/phash) have not been computed. trust='thumb_only'
    signals the dedup layer that a match here needs cross-validation via
    caption/file_size/duration before it can be treated as authoritative."""

    @pytest.mark.asyncio
    async def test_insert_thumb_record_sets_thumb_only_trust(self, mdb):
        mid = await mdb.insert_thumb_record(
            thumb_phash="abcd1234deadbeef",
            file_type="photo",
            file_size=12345,
            caption="sunset",
            duration=None,
            target_chat="@t",
            target_msg_id=500,
        )
        row = await mdb.get_media(mid)
        assert row["thumb_phash"] == "abcd1234deadbeef"
        assert row["trust"] == "thumb_only"
        assert row["sha256"] is None
        assert row["phash"] is None
        assert row["status"] == "uploaded"  # already in target
        assert row["target_chat"] == "@t"
        assert row["target_msg_id"] == 500
        assert row["source_chat"] == ""  # scan path has no source
        assert row["source_msg_id"] == 0

    @pytest.mark.asyncio
    async def test_insert_thumb_record_stores_duration_for_video(self, mdb):
        mid = await mdb.insert_thumb_record(
            thumb_phash="ff00ff00ff00ff00",
            file_type="video",
            file_size=500_000_000,
            caption="clip",
            duration=42,
            target_chat="@t",
            target_msg_id=501,
        )
        row = await mdb.get_media(mid)
        assert row["duration"] == 42
        assert row["file_type"] == "video"

    @pytest.mark.asyncio
    async def test_insert_thumb_record_is_idempotent_per_target_msg(self, mdb):
        """Re-scanning the same target message shouldn't create duplicate
        rows — keyed on (target_chat, target_msg_id)."""
        for _ in range(2):
            await mdb.insert_thumb_record(
                thumb_phash="aa00",
                file_type="photo",
                file_size=10,
                caption=None,
                duration=None,
                target_chat="@t",
                target_msg_id=700,
            )
        async with mdb._db.execute(
            "SELECT COUNT(*) as c FROM media WHERE target_chat='@t' "
            "AND target_msg_id=700"
        ) as cur:
            row = await cur.fetchone()
        assert row["c"] == 1


class TestFindByThumbPhash:
    @pytest.mark.asyncio
    async def test_find_by_thumb_phash_exact_match(self, mdb):
        await mdb.insert_thumb_record(
            thumb_phash="1111222233334444",
            file_type="photo", file_size=100, caption="a",
            duration=None, target_chat="@t", target_msg_id=1,
        )
        await mdb.insert_thumb_record(
            thumb_phash="ffffffffffffffff",
            file_type="photo", file_size=200, caption="b",
            duration=None, target_chat="@t", target_msg_id=2,
        )
        hits = await mdb.find_by_thumb_phash("1111222233334444", "@t")
        assert len(hits) == 1
        assert hits[0]["target_msg_id"] == 1

    @pytest.mark.asyncio
    async def test_find_by_thumb_phash_scoped_to_target(self, mdb):
        await mdb.insert_thumb_record(
            thumb_phash="aaaa", file_type="photo", file_size=1,
            caption=None, duration=None,
            target_chat="@t1", target_msg_id=1,
        )
        await mdb.insert_thumb_record(
            thumb_phash="aaaa", file_type="photo", file_size=1,
            caption=None, duration=None,
            target_chat="@t2", target_msg_id=1,
        )
        hits = await mdb.find_by_thumb_phash("aaaa", "@t1")
        assert len(hits) == 1
        assert hits[0]["target_chat"] == "@t1"


class TestPhase4Dedup:
    @pytest.mark.asyncio
    async def test_upgrade_thumb_to_full(self, mdb):
        media_id = await mdb.insert_thumb_record(
            thumb_phash="aaaa", file_type="photo", file_size=1,
            caption="c", duration=None,
            target_chat="@t1", target_msg_id=1,
        )
        before = await mdb.get_media(media_id)
        assert before["trust"] == "thumb_only"
        assert before["verified_by"] is None

        await mdb.upgrade_thumb_to_full(media_id, verified_by="metadata")

        after = await mdb.get_media(media_id)
        assert after["trust"] == "full"
        assert after["verified_by"] == "metadata"
        # sha256/phash intentionally untouched — we never downloaded the file
        assert after["sha256"] is None
        assert after["phash"] is None

    @pytest.mark.asyncio
    async def test_pending_dedup_insert_and_list(self, mdb):
        row_id = await mdb.insert_pending_dedup(
            job_id="job-1", source_chat="@src", source_msg_id=42,
            candidate_target_msg_ids=[100, 101, 102],
            reason="thumb_match_metadata_mismatch",
        )
        assert row_id > 0

        rows = await mdb.list_pending_dedup_by_job("job-1")
        assert len(rows) == 1
        assert rows[0]["source_msg_id"] == 42
        assert rows[0]["candidate_target_msg_ids"] == [100, 101, 102]
        assert rows[0]["reason"] == "thumb_match_metadata_mismatch"

    @pytest.mark.asyncio
    async def test_pending_dedup_scoped_by_job(self, mdb):
        await mdb.insert_pending_dedup(
            job_id="job-a", source_chat="@src", source_msg_id=1,
            candidate_target_msg_ids=[10], reason="r",
        )
        await mdb.insert_pending_dedup(
            job_id="job-b", source_chat="@src", source_msg_id=2,
            candidate_target_msg_ids=[20], reason="r",
        )
        rows = await mdb.list_pending_dedup_by_job("job-a")
        assert len(rows) == 1
        assert rows[0]["source_msg_id"] == 1

    @pytest.mark.asyncio
    async def test_pending_dedup_delete(self, mdb):
        row_id = await mdb.insert_pending_dedup(
            job_id="job-1", source_chat="@src", source_msg_id=42,
            candidate_target_msg_ids=[100], reason="r",
        )
        await mdb.delete_pending_dedup(row_id)
        rows = await mdb.list_pending_dedup_by_job("job-1")
        assert rows == []


class TestDeferredDedup:
    """Phase 6: deferred queue stores source-chat metadata recorded by
    `/batch --skip-dedup`. `/process_deferred` later drains the queue and
    routes each row to upload / dedup / pending_dedup."""

    @pytest.mark.asyncio
    async def test_insert_and_list(self, mdb):
        await mdb.insert_deferred_dedup(
            source_chat="@src", source_msg_id=10, target_chat="@tgt",
            thumb_phash="abc", file_type="photo", file_size=1234,
            caption="hi", duration=None, grouped_id=None,
        )
        rows = await mdb.list_deferred_dedup()
        assert len(rows) == 1
        assert rows[0]["source_msg_id"] == 10
        assert rows[0]["thumb_phash"] == "abc"
        assert rows[0]["caption"] == "hi"

    @pytest.mark.asyncio
    async def test_insert_replace_on_conflict(self, mdb):
        """Re-running a defer-scan over the same (src, msg, target) should
        refresh the row in place, not create a duplicate."""
        await mdb.insert_deferred_dedup(
            source_chat="@src", source_msg_id=10, target_chat="@tgt",
            thumb_phash="old", file_type="photo", file_size=100,
            caption="v1", duration=None, grouped_id=None,
        )
        await mdb.insert_deferred_dedup(
            source_chat="@src", source_msg_id=10, target_chat="@tgt",
            thumb_phash="new", file_type="photo", file_size=100,
            caption="v2", duration=None, grouped_id=None,
        )
        rows = await mdb.list_deferred_dedup()
        assert len(rows) == 1
        assert rows[0]["thumb_phash"] == "new"
        assert rows[0]["caption"] == "v2"

    @pytest.mark.asyncio
    async def test_list_scoped_by_source_and_target(self, mdb):
        await mdb.insert_deferred_dedup(
            source_chat="@a", source_msg_id=1, target_chat="@x",
            thumb_phash=None, file_type="document", file_size=None,
            caption=None, duration=None, grouped_id=None,
        )
        await mdb.insert_deferred_dedup(
            source_chat="@a", source_msg_id=2, target_chat="@y",
            thumb_phash=None, file_type="document", file_size=None,
            caption=None, duration=None, grouped_id=None,
        )
        await mdb.insert_deferred_dedup(
            source_chat="@b", source_msg_id=3, target_chat="@x",
            thumb_phash=None, file_type="document", file_size=None,
            caption=None, duration=None, grouped_id=None,
        )
        scoped = await mdb.list_deferred_dedup(
            source_chat="@a", target_chat="@x",
        )
        assert len(scoped) == 1
        assert scoped[0]["source_msg_id"] == 1

    @pytest.mark.asyncio
    async def test_count(self, mdb):
        assert await mdb.count_deferred_dedup() == 0
        await mdb.insert_deferred_dedup(
            source_chat="@s", source_msg_id=1, target_chat="@t",
            thumb_phash=None, file_type="photo", file_size=None,
            caption=None, duration=None, grouped_id=None,
        )
        assert await mdb.count_deferred_dedup() == 1

    @pytest.mark.asyncio
    async def test_delete(self, mdb):
        row_id = await mdb.insert_deferred_dedup(
            source_chat="@s", source_msg_id=1, target_chat="@t",
            thumb_phash=None, file_type="photo", file_size=None,
            caption=None, duration=None, grouped_id=None,
        )
        await mdb.delete_deferred_dedup(row_id)
        assert await mdb.count_deferred_dedup() == 0


@pytest.mark.asyncio
async def test_migration_adds_last_updated_at_with_value_from_created_at(tmp_path):
    """A legacy media row with last_checked_at=NULL must end up with
    last_updated_at = created_at after migration."""
    import aiosqlite
    db_path = str(tmp_path / "legacy.db")

    # Hand-build a legacy schema row
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE media (
                media_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT, phash TEXT, file_type TEXT NOT NULL,
                file_size INTEGER, caption TEXT,
                source_chat TEXT NOT NULL, source_msg_id INTEGER NOT NULL,
                target_chat TEXT NOT NULL, target_msg_id INTEGER,
                status TEXT DEFAULT 'pending', job_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked_at TIMESTAMP
            )
            """
        )
        await conn.execute(
            "INSERT INTO media (sha256, file_type, source_chat, source_msg_id, "
            "target_chat, status, created_at) VALUES "
            "('s1', 'photo', 's', 1, 't', 'uploaded', '2024-01-01 00:00:00')"
        )
        await conn.commit()

    # Run init → triggers migration
    mdb = MediaDB(db_path)
    await mdb.init()
    try:
        async with mdb._db.execute("PRAGMA table_info(media)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "last_updated_at" in cols
        async with mdb._db.execute(
            "SELECT last_updated_at, created_at FROM media WHERE source_msg_id=1"
        ) as cur:
            row = await cur.fetchone()
        assert row["last_updated_at"] == row["created_at"]
    finally:
        await mdb.close()


@pytest.mark.asyncio
async def test_migration_drops_last_checked_at_column_when_supported(tmp_path):
    """On SQLite >= 3.35, last_checked_at must be physically removed."""
    import sqlite3
    sqlite_version = tuple(int(x) for x in sqlite3.sqlite_version.split('.'))

    mdb = MediaDB(str(tmp_path / "x.db"))
    await mdb.init()
    try:
        async with mdb._db.execute("PRAGMA table_info(media)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if sqlite_version >= (3, 35, 0):
            assert "last_checked_at" not in cols
        # Always present in either case:
        assert "last_updated_at" in cols
    finally:
        await mdb.close()


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path):
    """Running init twice must not fail or re-create columns."""
    db_path = str(tmp_path / "y.db")
    mdb1 = MediaDB(db_path)
    await mdb1.init()
    await mdb1.close()
    mdb2 = MediaDB(db_path)
    await mdb2.init()  # must not raise
    try:
        async with mdb2._db.execute("PRAGMA table_info(media)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "last_updated_at" in cols
    finally:
        await mdb2.close()


@pytest.mark.asyncio
async def test_list_all_uploaded_ids_returns_only_uploaded(mdb):
    pending = await mdb.insert_media(
        sha256="p1", phash=None, file_type="photo", file_size=10,
        caption=None, source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    uploaded = await mdb.insert_media(
        sha256="u1", phash=None, file_type="photo", file_size=10,
        caption=None, source_chat="s", source_msg_id=2,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(uploaded, target_msg_id=200)
    failed = await mdb.insert_media(
        sha256="f1", phash=None, file_type="photo", file_size=10,
        caption=None, source_chat="s", source_msg_id=3,
        target_chat="t", job_id="j",
    )
    await mdb.mark_failed(failed)

    ids = await mdb.list_all_uploaded_ids()
    assert ids == [uploaded]


@pytest.mark.asyncio
async def test_update_caption_and_tags_replaces_caption_and_tags(mdb):
    media_id = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption="old #foo #bar", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(media_id, target_msg_id=10)
    await mdb.add_tags(media_id, ["foo", "bar"])

    await mdb.update_caption_and_tags(media_id, "new #baz")

    row = await mdb.get_media(media_id)
    assert row["caption"] == "new #baz"
    tags = await mdb.get_tags(media_id)
    assert tags == ["baz"]


@pytest.mark.asyncio
async def test_init_enables_wal_mode_media(tmp_path):
    mdb = MediaDB(str(tmp_path / "wal.db"))
    await mdb.init()
    try:
        async with mdb._db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row[0].lower() == "wal"
    finally:
        await mdb.close()


@pytest.mark.asyncio
async def test_phash_lookup_index_exists(mdb):
    async with mdb._db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_media_phash_lookup'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_target_msg_index_exists(mdb):
    async with mdb._db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_media_target_msg'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_update_caption_and_tags_bumps_last_updated_at(mdb):
    media_id = await mdb.insert_media(
        sha256="b", phash=None, file_type="photo", file_size=1,
        caption="old", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(media_id, target_msg_id=10)
    before = (await mdb.get_media(media_id))["last_updated_at"]
    # Sleep a tick so timestamp comparison is meaningful at second resolution
    import asyncio
    await asyncio.sleep(1.05)
    await mdb.update_caption_and_tags(media_id, "new content")
    after = (await mdb.get_media(media_id))["last_updated_at"]
    assert after > before


@pytest.mark.asyncio
async def test_search_keyword_uses_sql_pagination(mdb):
    """Inserting > page_size matches: page1 returns page_size, page2
    returns the remainder, no overlap."""
    for i in range(15):
        m = await mdb.insert_media(
            sha256=f"h{i}", phash=None, file_type="photo", file_size=1,
            caption=f"hello world {i}", source_chat="s", source_msg_id=i,
            target_chat="t", job_id="j",
        )
        await mdb.mark_uploaded(m, target_msg_id=1000 + i)

    page1, total1 = await mdb.search_keyword("hello", page=1, page_size=10)
    page2, total2 = await mdb.search_keyword("hello", page=2, page_size=10)

    assert total1 == 15
    assert total2 == 15
    assert len(page1) == 10
    assert len(page2) == 5
    assert {r["media_id"] for r in page1}.isdisjoint(
        {r["media_id"] for r in page2}
    )
