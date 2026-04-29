import asyncio
import json
import os

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agents.tg_transfer.liveness_checker import (
    LIVENESS_DIR,
    create_plan,
    load_plan,
    save_plan,
    locate_or_create_plan,
    process_one,
    run_one_scan,
)
from agents.tg_transfer.media_db import MediaDB


@pytest_asyncio.fixture
async def mdb(tmp_path):
    db = MediaDB(str(tmp_path / "liveness_test.db"))
    await db.init()
    yield db
    await db.close()


def _liveness_root(tmp_path):
    return str(tmp_path / "tmp")


@pytest.mark.asyncio
async def test_create_plan_writes_file_with_all_uploaded_ids(mdb, tmp_path):
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption=None, source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    m2 = await mdb.insert_media(
        sha256="b", phash=None, file_type="photo", file_size=1,
        caption=None, source_chat="s", source_msg_id=2,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=10)
    await mdb.mark_uploaded(m2, target_msg_id=11)

    root = _liveness_root(tmp_path)
    path = await create_plan(mdb, tmp_root=root)

    assert os.path.exists(path)
    assert os.path.dirname(path).endswith(LIVENESS_DIR)
    plan = load_plan(path)
    assert set(plan["remaining"]) == {m1, m2}
    assert "scan_id" in plan
    assert "started_at" in plan


def test_save_plan_uses_atomic_rename(tmp_path):
    """save_plan must write to a .tmp file then rename, so a crash mid-write
    leaves the previous (good) file in place."""
    root = str(tmp_path / "tmp")
    os.makedirs(os.path.join(root, LIVENESS_DIR), exist_ok=True)
    path = os.path.join(root, LIVENESS_DIR, "abc.json")
    save_plan(path, {"scan_id": "abc", "started_at": "x", "remaining": [1, 2, 3]})

    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    assert load_plan(path)["remaining"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_locate_or_create_plan_resumes_existing(mdb, tmp_path):
    """If a plan file already exists, locate_or_create_plan must return it
    untouched instead of creating a new one (this is how restart-resume
    works)."""
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption=None, source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=10)

    root = _liveness_root(tmp_path)
    os.makedirs(os.path.join(root, LIVENESS_DIR), exist_ok=True)
    pre_existing = os.path.join(root, LIVENESS_DIR, "pre.json")
    save_plan(pre_existing, {
        "scan_id": "pre", "started_at": "earlier", "remaining": [999],
    })

    found = await locate_or_create_plan(mdb, tmp_root=root)
    assert found == pre_existing
    plan = load_plan(found)
    assert plan["remaining"] == [999]   # NOT replaced with current uploaded ids


@pytest.mark.asyncio
async def test_process_one_deletes_when_message_missing(mdb, tmp_path):
    """If client.get_messages returns None, process_one must delete the
    media row."""
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption=None, source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=50)

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=None)

    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await process_one(client, mdb, m1)

    assert await mdb.get_media(m1) is None


@pytest.mark.asyncio
async def test_process_one_updates_caption_when_changed(mdb, tmp_path):
    """If the target message's caption has changed, process_one must
    update caption + tags + last_updated_at."""
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption="old #foo", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=50)
    await mdb.add_tags(m1, ["foo"])

    msg = MagicMock()
    msg.id = 50
    msg.text = "new caption #bar"
    msg.message = "new caption #bar"

    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=msg)

    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await process_one(client, mdb, m1)

    row = await mdb.get_media(m1)
    assert row["caption"] == "new caption #bar"
    assert await mdb.get_tags(m1) == ["bar"]


@pytest.mark.asyncio
async def test_process_one_no_op_when_caption_unchanged(mdb, tmp_path):
    """If the target message's caption matches the stored one, process_one
    must not bump last_updated_at and must keep the row + tags intact."""
    m1 = await mdb.insert_media(
        sha256="a", phash=None, file_type="photo", file_size=1,
        caption="same #x", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m1, target_msg_id=50)
    await mdb.add_tags(m1, ["x"])
    before = (await mdb.get_media(m1))["last_updated_at"]
    await asyncio.sleep(1.05)

    msg = MagicMock()
    msg.id = 50
    msg.text = "same #x"
    msg.message = "same #x"
    client = AsyncMock()
    client.get_messages = AsyncMock(return_value=msg)

    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await process_one(client, mdb, m1)

    after = (await mdb.get_media(m1))["last_updated_at"]
    assert after == before


@pytest.mark.asyncio
async def test_process_one_skips_when_row_already_gone(mdb, tmp_path):
    """If the media row was removed by something else (e.g. concurrent
    on_task_deleted), process_one must safely skip without raising."""
    client = AsyncMock()
    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        # 99999 doesn't exist — must not raise
        await process_one(client, mdb, 99999)
    # client.get_messages must not have been called
    client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_run_one_scan_processes_all_and_deletes_plan(mdb, tmp_path):
    """End-to-end: run_one_scan iterates pop-50 until empty, then deletes
    the plan file."""
    ids = []
    for i in range(3):
        mid = await mdb.insert_media(
            sha256=f"h{i}", phash=None, file_type="photo", file_size=1,
            caption=f"c{i}", source_chat="s", source_msg_id=100 + i,
            target_chat="t", job_id="j",
        )
        await mdb.mark_uploaded(mid, target_msg_id=200 + i)
        ids.append(mid)

    msg = MagicMock()
    # Make all messages "alive" with unchanged caption
    def make_msg(target_msg_id):
        m = MagicMock()
        m.id = target_msg_id
        # Stored captions are "c0"/"c1"/"c2"
        idx = target_msg_id - 200
        m.text = f"c{idx}"
        m.message = f"c{idx}"
        return m

    async def fake_get_messages(entity, ids):
        return make_msg(ids)

    client = AsyncMock()
    client.get_messages = AsyncMock(side_effect=fake_get_messages)

    root = _liveness_root(tmp_path)
    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await run_one_scan(client, mdb, tmp_root=root)

    # All rows still present (alive + unchanged)
    for mid in ids:
        assert await mdb.get_media(mid) is not None
    # Plan file deleted
    liveness_dir = os.path.join(root, LIVENESS_DIR)
    assert not os.path.exists(liveness_dir) or os.listdir(liveness_dir) == []


@pytest.mark.asyncio
async def test_run_one_scan_resumes_from_existing_plan(mdb, tmp_path):
    """If a plan file is already there, run_one_scan must drain IT, not
    rebuild from current uploaded ids. Verifies restart-resume."""
    # Insert one media row but DO NOT include it in the pre-existing plan
    m_real = await mdb.insert_media(
        sha256="real", phash=None, file_type="photo", file_size=1,
        caption="c", source_chat="s", source_msg_id=1,
        target_chat="t", job_id="j",
    )
    await mdb.mark_uploaded(m_real, target_msg_id=500)

    # Stale plan only mentions a non-existent media_id
    root = _liveness_root(tmp_path)
    liveness_dir = os.path.join(root, LIVENESS_DIR)
    os.makedirs(liveness_dir, exist_ok=True)
    plan_path = os.path.join(liveness_dir, "stale.json")
    save_plan(plan_path, {
        "scan_id": "stale", "started_at": "x", "remaining": [99999],
    })

    client = AsyncMock()
    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await run_one_scan(client, mdb, tmp_root=root)

    # The real row must still be uploaded — it was NOT in the plan, so
    # the scan never touched it.
    assert await mdb.get_media(m_real) is not None
    # The plan is consumed (99999 silently skipped via process_one's
    # guard) and deleted.
    assert os.listdir(liveness_dir) == []
    # client.get_messages never called — 99999 doesn't resolve to a row
    client.get_messages.assert_not_called()


@pytest.mark.asyncio
async def test_run_one_scan_atomic_rewrite_after_each_batch(mdb, tmp_path, monkeypatch):
    """After each pop-of-50, the plan file must be atomically rewritten
    with the trimmed remaining list. Verify by intercepting save_plan."""
    # Force batch size = 2 for this test
    from agents.tg_transfer import liveness_checker
    monkeypatch.setattr(liveness_checker, "BATCH_SIZE", 2)

    ids = []
    for i in range(5):
        mid = await mdb.insert_media(
            sha256=f"h{i}", phash=None, file_type="photo", file_size=1,
            caption=f"c{i}", source_chat="s", source_msg_id=100 + i,
            target_chat="t", job_id="j",
        )
        await mdb.mark_uploaded(mid, target_msg_id=200 + i)
        ids.append(mid)

    saves = []
    real_save_plan = liveness_checker.save_plan

    def spy(path, plan):
        saves.append(list(plan["remaining"]))
        real_save_plan(path, plan)
    monkeypatch.setattr(liveness_checker, "save_plan", spy)

    msg = MagicMock()
    msg.id = 0
    msg.text = ""
    msg.message = ""
    async def fake_get_messages(entity, ids):
        m = MagicMock()
        m.id = ids
        m.text = f"c{ids - 200}"
        m.message = f"c{ids - 200}"
        return m
    client = AsyncMock()
    client.get_messages = AsyncMock(side_effect=fake_get_messages)

    root = _liveness_root(tmp_path)
    with patch(
        "agents.tg_transfer.liveness_checker.resolve_chat",
        AsyncMock(return_value="entity"),
    ):
        await run_one_scan(client, mdb, tmp_root=root)

    # First save (initial create_plan): 5 ids
    assert saves[0] == ids
    # After first pop-2: 3 remaining
    assert saves[1] == ids[2:]
    # After pop-2 again: 1 remaining
    assert saves[2] == ids[4:]
    # After pop-1 (or pop-2 with only 1 left): 0 remaining
    assert saves[3] == []
