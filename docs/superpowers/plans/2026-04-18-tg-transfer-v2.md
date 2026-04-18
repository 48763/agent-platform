# TG Transfer Agent v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add media hash dedup (SHA-256 + pHash), keyword/image search with pagination, tag system, stats dashboard, and liveness checking to the existing TG Transfer Agent.

**Architecture:** New modules (media_db, hasher, search, tag_extractor, liveness_checker, dashboard) are independent units that plug into the existing transfer_engine and __main__.py. The existing db.py and transfer flow are modified minimally — media tracking lives in its own DB layer.

**Tech Stack:** Python 3.12, aiosqlite, imagehash, Pillow, ffmpeg (apk), Telethon

---

## File Structure

```
agents/tg_transfer/
├── media_db.py          # NEW: media/tags/media_tags table operations
├── hasher.py            # NEW: SHA-256 + pHash calculation, Hamming distance
├── tag_extractor.py     # NEW: extract #tags from caption
├── search.py            # NEW: keyword search, image search, pagination
├── liveness_checker.py  # NEW: background liveness check task
├── dashboard.py         # NEW: stats HTML endpoint
├── transfer_engine.py   # MODIFY: integrate hash dedup + media recording + tags
├── __main__.py          # MODIFY: add search dispatch, start liveness checker, register dashboard route
├── parser.py            # MODIFY: add search/stats intent classification
├── agent.yaml           # MODIFY: add new settings
├── Dockerfile           # MODIFY: add Pillow, imagehash, ffmpeg
tests/
├── test_hasher.py       # NEW
├── test_tag_extractor.py # NEW
├── test_media_db.py     # NEW
├── test_search.py       # NEW
├── test_liveness.py     # NEW
```

---

### Task 1: Dependencies + Dockerfile

**Files:**
- Modify: `agents/tg_transfer/Dockerfile`
- Modify: `agents/tg_transfer/agent.yaml`

- [ ] **Step 1: Update Dockerfile**

Replace the entire Dockerfile content:

```dockerfile
FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev ffmpeg

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir aiosqlite>=0.20 imagehash>=4.3 Pillow>=10

COPY core/ ./core/
COPY agents/ ./agents/

CMD ["python", "-m", "agents.tg_transfer"]
```

- [ ] **Step 2: Update agent.yaml with new settings**

```yaml
name: tg-transfer-agent
description: "Telegram 群組資源搬移工具：支援單則/批量搬移、斷點續傳、跨 Job 去重、媒體搜尋"
priority: 3
route_patterns:
  - "搬移|轉存|複製群組|搬到|copy to|transfer|備份群組|搜尋|查詢|search|統計|stats"
sandbox:
  allowed_dirs: []
  writable: false
settings:
  default_target_chat: ""
  retry_limit: 3
  progress_interval: 20
  telethon_session: "tg_transfer"
  liveness_check_interval: 24
  search_page_size: 10
  phash_threshold: 10
```

- [ ] **Step 3: Commit**

```bash
git add agents/tg_transfer/Dockerfile agents/tg_transfer/agent.yaml
git commit -m "chore(tg-transfer): add imagehash, Pillow, ffmpeg deps and v2 settings"
```

---

### Task 2: Hasher — SHA-256 + pHash

**Files:**
- Create: `agents/tg_transfer/hasher.py`
- Create: `tests/test_hasher.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_hasher.py
import pytest
import os
import tempfile
from PIL import Image
from agents.tg_transfer.hasher import compute_sha256, compute_phash, hamming_distance, PHASH_AVAILABLE


class TestSHA256:
    def test_compute_sha256(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"hello world")
        result = compute_sha256(str(f))
        assert len(result) == 64  # hex string
        assert result == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"identical")
        f2.write_bytes(b"identical")
        assert compute_sha256(str(f1)) == compute_sha256(str(f2))

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"aaa")
        f2.write_bytes(b"bbb")
        assert compute_sha256(str(f1)) != compute_sha256(str(f2))


class TestPHash:
    def test_compute_phash_returns_hex_string(self, tmp_path):
        img = Image.new("RGB", (100, 100), color="red")
        path = str(tmp_path / "red.png")
        img.save(path)
        result = compute_phash(path)
        assert result is not None
        assert len(result) == 16  # 64-bit as 16 hex chars

    def test_same_image_same_phash(self, tmp_path):
        img = Image.new("RGB", (100, 100), color="blue")
        p1 = str(tmp_path / "a.png")
        p2 = str(tmp_path / "b.png")
        img.save(p1)
        img.save(p2)
        assert compute_phash(p1) == compute_phash(p2)

    def test_non_image_returns_none(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("not an image")
        result = compute_phash(str(f))
        assert result is None


class TestHammingDistance:
    def test_identical(self):
        assert hamming_distance("abcdef0123456789", "abcdef0123456789") == 0

    def test_one_bit_diff(self):
        assert hamming_distance("0000000000000000", "0000000000000001") == 1

    def test_all_bits_diff(self):
        assert hamming_distance("0000000000000000", "ffffffffffffffff") == 64
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hasher.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement hasher.py**

```python
# agents/tg_transfer/hasher.py
import hashlib
import logging
import asyncio
import os

logger = logging.getLogger(__name__)

try:
    import imagehash
    from PIL import Image
    PHASH_AVAILABLE = True
except ImportError:
    PHASH_AVAILABLE = False
    logger.warning("imagehash/Pillow not available, pHash disabled")


def compute_sha256(file_path: str) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_phash(file_path: str) -> str | None:
    """Compute perceptual hash of an image file. Returns 16-char hex string or None."""
    if not PHASH_AVAILABLE:
        return None
    try:
        img = Image.open(file_path)
        return str(imagehash.phash(img))
    except Exception as e:
        logger.debug(f"pHash failed for {file_path}: {e}")
        return None


async def compute_phash_video(file_path: str, tmp_dir: str) -> str | None:
    """Extract first frame from video with ffmpeg, then compute pHash."""
    if not PHASH_AVAILABLE:
        return None
    frame_path = os.path.join(tmp_dir, "frame.jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", file_path, "-ss", "1", "-frames:v", "1",
            "-y", frame_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and os.path.exists(frame_path):
            result = compute_phash(frame_path)
            os.remove(frame_path)
            return result
    except Exception as e:
        logger.debug(f"Video pHash failed for {file_path}: {e}")
    return None


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two hex hash strings."""
    return bin(int(hash1, 16) ^ int(hash2, 16)).count("1")
```

- [ ] **Step 4: Install deps and run tests**

Run: `pip3 install --break-system-packages imagehash Pillow && python3 -m pytest tests/test_hasher.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/hasher.py tests/test_hasher.py
git commit -m "feat(tg-transfer): add SHA-256 and pHash computation module"
```

---

### Task 3: Tag Extractor

**Files:**
- Create: `agents/tg_transfer/tag_extractor.py`
- Create: `tests/test_tag_extractor.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tag_extractor.py
import pytest
from agents.tg_transfer.tag_extractor import extract_tags


class TestExtractTags:
    def test_single_tag(self):
        assert extract_tags("這是 #教學 影片") == ["教學"]

    def test_multiple_tags(self):
        assert extract_tags("#影片 #教學 #python") == ["影片", "教學", "python"]

    def test_no_tags(self):
        assert extract_tags("這是一段普通文字") == []

    def test_none_input(self):
        assert extract_tags(None) == []

    def test_empty_string(self):
        assert extract_tags("") == []

    def test_tag_with_chinese(self):
        assert extract_tags("#測試 #資料備份") == ["測試", "資料備份"]

    def test_dedup_tags(self):
        assert extract_tags("#aaa #bbb #aaa") == ["aaa", "bbb"]

    def test_tag_at_start(self):
        assert extract_tags("#開頭標籤 一些文字") == ["開頭標籤"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tag_extractor.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement tag_extractor.py**

```python
# agents/tg_transfer/tag_extractor.py
import re

_TAG_RE = re.compile(r"#(\w+)", re.UNICODE)


def extract_tags(text: str | None) -> list[str]:
    """Extract unique #tags from text, preserving order of first appearance."""
    if not text:
        return []
    seen = set()
    tags = []
    for m in _TAG_RE.finditer(text):
        tag = m.group(1)
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_tag_extractor.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/tag_extractor.py tests/test_tag_extractor.py
git commit -m "feat(tg-transfer): add tag extractor for #hashtag parsing"
```

---

### Task 4: Media DB Layer

**Files:**
- Create: `agents/tg_transfer/media_db.py`
- Create: `tests/test_media_db.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_media_db.py
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
    await mdb.insert_media(
        sha256="dup", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=10, target_chat="@dst", job_id="j1",
    )
    await mdb.mark_uploaded(1, target_msg_id=50)
    found = await mdb.find_by_sha256("dup", "@dst")
    assert found is not None
    assert found["status"] == "uploaded"


@pytest.mark.asyncio
async def test_find_by_sha256_not_found(mdb):
    found = await mdb.find_by_sha256("nonexistent", "@dst")
    assert found is None


@pytest.mark.asyncio
async def test_find_similar_phash(mdb):
    await mdb.insert_media(
        sha256="x1", phash="0000000000000000", file_type="photo",
        file_size=100, caption="similar photo", source_chat="@s",
        source_msg_id=20, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(1, target_msg_id=60)
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
async def test_get_stale_media(mdb):
    m1 = await mdb.insert_media(
        sha256="stale1", phash=None, file_type="photo",
        file_size=100, caption=None, source_chat="@s",
        source_msg_id=70, target_chat="@d", job_id="j1",
    )
    await mdb.mark_uploaded(m1, target_msg_id=100)
    # last_checked_at is NULL, so it should be stale
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_media_db.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement media_db.py**

```python
# agents/tg_transfer/media_db.py
import aiosqlite
from typing import Optional

_MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    media_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256          TEXT NOT NULL,
    phash           TEXT,
    file_type       TEXT NOT NULL,
    file_size       INTEGER,
    caption         TEXT,
    source_chat     TEXT NOT NULL,
    source_msg_id   INTEGER NOT NULL,
    target_chat     TEXT NOT NULL,
    target_msg_id   INTEGER,
    status          TEXT DEFAULT 'pending',
    job_id          TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_checked_at TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_media_sha256_target
    ON media(sha256, target_chat);
CREATE INDEX IF NOT EXISTS idx_media_phash ON media(phash);
CREATE INDEX IF NOT EXISTS idx_media_caption ON media(caption);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);

CREATE TABLE IF NOT EXISTS tags (
    tag_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS media_tags (
    media_id INTEGER NOT NULL,
    tag_id   INTEGER NOT NULL,
    PRIMARY KEY (media_id, tag_id),
    FOREIGN KEY (media_id) REFERENCES media(media_id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
);
"""


class MediaDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(_MEDIA_SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # -- Media CRUD --

    async def insert_media(
        self, sha256: str, phash: str | None, file_type: str, file_size: int | None,
        caption: str | None, source_chat: str, source_msg_id: int,
        target_chat: str, job_id: str | None = None,
    ) -> int:
        async with self._db.execute(
            "INSERT INTO media (sha256, phash, file_type, file_size, caption, "
            "source_chat, source_msg_id, target_chat, job_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sha256, phash, file_type, file_size, caption, source_chat,
             source_msg_id, target_chat, job_id),
        ) as cur:
            media_id = cur.lastrowid
        await self._db.commit()
        return media_id

    async def get_media(self, media_id: int) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM media WHERE media_id = ?", (media_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def mark_uploaded(self, media_id: int, target_msg_id: int):
        await self._db.execute(
            "UPDATE media SET status = 'uploaded', target_msg_id = ?, "
            "last_checked_at = CURRENT_TIMESTAMP WHERE media_id = ?",
            (target_msg_id, media_id),
        )
        await self._db.commit()

    async def mark_skipped(self, media_id: int):
        await self._db.execute(
            "UPDATE media SET status = 'skipped' WHERE media_id = ?", (media_id,)
        )
        await self._db.commit()

    async def delete_media(self, media_id: int):
        await self._db.execute("DELETE FROM media WHERE media_id = ?", (media_id,))
        await self._db.commit()

    # -- Dedup --

    async def find_by_sha256(self, sha256: str, target_chat: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM media WHERE sha256 = ? AND target_chat = ? "
            "AND status IN ('uploaded', 'pending') ORDER BY media_id DESC LIMIT 1",
            (sha256, target_chat),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_all_phashes(self) -> list[dict]:
        async with self._db.execute(
            "SELECT media_id, phash, caption, target_chat, target_msg_id "
            "FROM media WHERE phash IS NOT NULL AND status = 'uploaded'"
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    # -- Tags --

    async def add_tags(self, media_id: int, tag_names: list[str]):
        for name in tag_names:
            await self._db.execute(
                "INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,)
            )
            async with self._db.execute(
                "SELECT tag_id FROM tags WHERE name = ?", (name,)
            ) as cur:
                row = await cur.fetchone()
                tag_id = row["tag_id"]
            await self._db.execute(
                "INSERT OR IGNORE INTO media_tags (media_id, tag_id) VALUES (?, ?)",
                (media_id, tag_id),
            )
        await self._db.commit()

    async def get_tags(self, media_id: int) -> list[str]:
        async with self._db.execute(
            "SELECT t.name FROM tags t JOIN media_tags mt ON t.tag_id = mt.tag_id "
            "WHERE mt.media_id = ?", (media_id,)
        ) as cur:
            return [row["name"] for row in await cur.fetchall()]

    # -- Search --

    async def search_keyword(self, keyword: str, page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
        offset = (page - 1) * page_size
        # Search caption + tag name, union and dedup
        query = """
            SELECT DISTINCT m.media_id, m.caption, m.target_chat, m.target_msg_id, m.created_at
            FROM media m
            LEFT JOIN media_tags mt ON m.media_id = mt.media_id
            LEFT JOIN tags t ON mt.tag_id = t.tag_id
            WHERE m.status = 'uploaded' AND (m.caption LIKE ? OR t.name LIKE ?)
            ORDER BY m.created_at DESC
        """
        like = f"%{keyword}%"
        async with self._db.execute(query, (like, like)) as cur:
            all_rows = [dict(row) for row in await cur.fetchall()]
        total = len(all_rows)
        page_rows = all_rows[offset:offset + page_size]
        return page_rows, total

    # -- Stats --

    async def get_stats(self) -> dict:
        async with self._db.execute(
            "SELECT COUNT(*) as cnt FROM media WHERE status = 'uploaded'"
        ) as cur:
            total_media = (await cur.fetchone())["cnt"]
        async with self._db.execute("SELECT COUNT(*) as cnt FROM tags") as cur:
            total_tags = (await cur.fetchone())["cnt"]
        async with self._db.execute(
            "SELECT t.name, COUNT(mt.media_id) as cnt FROM tags t "
            "JOIN media_tags mt ON t.tag_id = mt.tag_id "
            "JOIN media m ON mt.media_id = m.media_id AND m.status = 'uploaded' "
            "GROUP BY t.name ORDER BY cnt DESC"
        ) as cur:
            tag_counts = [(row["name"], row["cnt"]) for row in await cur.fetchall()]
        return {"total_media": total_media, "total_tags": total_tags, "tag_counts": tag_counts}

    # -- Liveness --

    async def get_stale_media(self, max_age_hours: int = 24, limit: int = 50) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM media WHERE status = 'uploaded' "
            "AND (last_checked_at IS NULL OR last_checked_at < datetime('now', ? || ' hours')) "
            "LIMIT ?",
            (f"-{max_age_hours}", limit),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def update_last_checked(self, media_id: int):
        await self._db.execute(
            "UPDATE media SET last_checked_at = CURRENT_TIMESTAMP WHERE media_id = ?",
            (media_id,),
        )
        await self._db.commit()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_media_db.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/media_db.py tests/test_media_db.py
git commit -m "feat(tg-transfer): add media DB layer with tags, search, stats, liveness"
```

---

### Task 5: Search Module

**Files:**
- Create: `agents/tg_transfer/search.py`
- Create: `tests/test_search.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_search.py
import pytest
from agents.tg_transfer.search import format_search_results, format_similar_results


class TestFormatSearchResults:
    def test_format_results(self):
        results = [
            {"caption": "Python 教學影片，這是一段很長的描述文字用來測試截斷功能", "target_chat": "@dst", "target_msg_id": 123},
            {"caption": "Rust 教學", "target_chat": "@dst", "target_msg_id": 456},
        ]
        text = format_search_results(results, total=2, page=1, page_size=10)
        assert "Python 教學影片" in text
        assert "t.me" in text
        assert "1/1" in text  # page info

    def test_format_no_results(self):
        text = format_search_results([], total=0, page=1, page_size=10)
        assert "找不到" in text

    def test_format_pagination_info(self):
        results = [{"caption": f"item {i}", "target_chat": "@dst", "target_msg_id": i} for i in range(10)]
        text = format_search_results(results, total=25, page=1, page_size=10)
        assert "1/3" in text  # 25 items, 10 per page = 3 pages

    def test_none_caption(self):
        results = [{"caption": None, "target_chat": "@dst", "target_msg_id": 789}]
        text = format_search_results(results, total=1, page=1, page_size=10)
        assert "（無文字）" in text


class TestFormatSimilarResults:
    def test_format_similar(self):
        results = [
            {"caption": "相似圖片", "target_chat": "@dst", "target_msg_id": 100, "distance": 3},
        ]
        text = format_similar_results(results)
        assert "相似" in text
        assert "t.me" in text

    def test_no_similar(self):
        text = format_similar_results([])
        assert "沒有找到" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_search.py -v`
Expected: FAIL

- [ ] **Step 3: Implement search.py**

```python
# agents/tg_transfer/search.py
import math


def _make_link(target_chat: str, target_msg_id: int) -> str:
    """Generate a TG message link."""
    chat = target_chat.lstrip("@")
    return f"https://t.me/{chat}/{target_msg_id}"


def _truncate(text: str | None, max_len: int = 50) -> str:
    if not text:
        return "（無文字）"
    return text[:max_len] + "..." if len(text) > max_len else text


def format_search_results(results: list[dict], total: int, page: int, page_size: int) -> str:
    """Format search results with pagination info."""
    if not results:
        return "找不到符合條件的媒體"

    total_pages = math.ceil(total / page_size)
    lines = [f"搜尋結果（{page}/{total_pages} 頁，共 {total} 筆）\n"]

    for i, r in enumerate(results, start=(page - 1) * page_size + 1):
        preview = _truncate(r["caption"])
        link = _make_link(r["target_chat"], r["target_msg_id"])
        lines.append(f"{i}. {preview}\n   {link}")

    if page < total_pages:
        lines.append(f"\n輸入「下一頁」查看更多")

    return "\n".join(lines)


def format_similar_results(results: list[dict]) -> str:
    """Format pHash similar search results."""
    if not results:
        return "沒有找到相似的媒體"

    lines = ["找到以下相似媒體：\n"]
    for i, r in enumerate(results, 1):
        preview = _truncate(r["caption"])
        link = _make_link(r["target_chat"], r["target_msg_id"])
        distance = r.get("distance", "?")
        lines.append(f"{i}. {preview}（相似度差異：{distance}）\n   {link}")

    lines.append("\n是否仍要上傳？（是/否）")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_search.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/search.py tests/test_search.py
git commit -m "feat(tg-transfer): add search result formatting with pagination"
```

---

### Task 6: Liveness Checker

**Files:**
- Create: `agents/tg_transfer/liveness_checker.py`
- Create: `tests/test_liveness.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_liveness.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_liveness.py -v`
Expected: FAIL

- [ ] **Step 3: Implement liveness_checker.py**

```python
# agents/tg_transfer/liveness_checker.py
import asyncio
import logging
from telethon import TelegramClient
from agents.tg_transfer.media_db import MediaDB
from agents.tg_transfer.chat_resolver import resolve_chat

logger = logging.getLogger(__name__)


async def check_batch(
    client: TelegramClient, media_db: MediaDB, media_list: list[dict]
) -> tuple[int, int]:
    """Check a batch of media for liveness. Returns (deleted_count, checked_count)."""
    deleted = 0
    checked = 0
    for media in media_list:
        try:
            entity = await resolve_chat(client, media["target_chat"])
            msg = await client.get_messages(entity, ids=media["target_msg_id"])
            if msg is None:
                await media_db.delete_media(media["media_id"])
                deleted += 1
                logger.info(f"Media {media['media_id']} dead, deleted")
            else:
                await media_db.update_last_checked(media["media_id"])
            checked += 1
        except Exception as e:
            logger.error(f"Liveness check failed for media {media['media_id']}: {e}")
            checked += 1
    return deleted, checked


async def run_liveness_loop(
    client: TelegramClient, media_db: MediaDB, interval_hours: int = 24
):
    """Background loop that periodically checks media liveness."""
    interval_secs = interval_hours * 3600
    while True:
        try:
            stale = await media_db.get_stale_media(max_age_hours=interval_hours, limit=50)
            if stale:
                deleted, checked = await check_batch(client, media_db, stale)
                logger.info(f"Liveness check: {checked} checked, {deleted} deleted")
        except Exception as e:
            logger.error(f"Liveness loop error: {e}")
        await asyncio.sleep(interval_secs)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_liveness.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/liveness_checker.py tests/test_liveness.py
git commit -m "feat(tg-transfer): add background liveness checker for media"
```

---

### Task 7: Dashboard Endpoint

**Files:**
- Create: `agents/tg_transfer/dashboard.py`

- [ ] **Step 1: Implement dashboard.py**

```python
# agents/tg_transfer/dashboard.py
from aiohttp import web
from agents.tg_transfer.media_db import MediaDB


async def dashboard_handler(request: web.Request) -> web.Response:
    """Serve stats dashboard HTML page."""
    media_db: MediaDB = request.app["media_db"]
    stats = await media_db.get_stats()

    tag_rows = ""
    for name, count in stats["tag_counts"]:
        tag_rows += f"<tr><td>{name}</td><td>{count}</td></tr>\n"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>TG Transfer Stats</title>
<style>
body {{ font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 0 20px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background: #f5f5f5; }}
.stat {{ font-size: 2em; font-weight: bold; color: #333; }}
.stat-label {{ color: #888; font-size: 0.9em; }}
.stats-row {{ display: flex; gap: 40px; margin: 20px 0; }}
</style></head>
<body>
<h1>TG Transfer 統計</h1>
<div class="stats-row">
  <div><div class="stat">{stats['total_media']}</div><div class="stat-label">儲存媒體數量</div></div>
  <div><div class="stat">{stats['total_tags']}</div><div class="stat-label">標籤總數</div></div>
</div>
<h2>標籤統計</h2>
<table>
<tr><th>標籤</th><th>媒體數量</th></tr>
{tag_rows if tag_rows else '<tr><td colspan="2">尚無標籤</td></tr>'}
</table>
</body></html>"""
    return web.Response(text=html, content_type="text/html")
```

- [ ] **Step 2: Commit**

```bash
git add agents/tg_transfer/dashboard.py
git commit -m "feat(tg-transfer): add stats dashboard HTML endpoint"
```

---

### Task 8: Integrate into Transfer Engine + __main__.py + Parser

**Files:**
- Modify: `agents/tg_transfer/transfer_engine.py`
- Modify: `agents/tg_transfer/__main__.py`
- Modify: `agents/tg_transfer/parser.py`

This is the largest task — it wires all new modules into the existing flow.

- [ ] **Step 1: Update parser.py — add search/stats intent**

Add to `agents/tg_transfer/parser.py`, after the existing `_CONFIG_RE`:

```python
_SEARCH_RE = re.compile(r"(搜尋|查詢|search|找)", re.IGNORECASE)
_STATS_RE = re.compile(r"(統計|stats)", re.IGNORECASE)
_PAGE_RE = re.compile(r"(下一頁|上一頁|next|prev)", re.IGNORECASE)
```

Replace the `classify_intent` function:

```python
def classify_intent(text: str) -> str:
    """Classify user message intent.
    Returns: 'single_transfer', 'config', 'search', 'stats', 'page', or 'batch'.
    """
    if parse_tg_link(text) is not None:
        return "single_transfer"
    if _CONFIG_RE.search(text):
        return "config"
    if _STATS_RE.search(text):
        return "stats"
    if _PAGE_RE.search(text):
        return "page"
    if _SEARCH_RE.search(text):
        return "search"
    return "batch"
```

- [ ] **Step 2: Update transfer_engine.py — add hash dedup + media recording**

Add imports at top of `agents/tg_transfer/transfer_engine.py`:

```python
from agents.tg_transfer.hasher import compute_sha256, compute_phash, compute_phash_video, hamming_distance, PHASH_AVAILABLE
from agents.tg_transfer.media_db import MediaDB
from agents.tg_transfer.tag_extractor import extract_tags
```

Add `media_db` and `phash_threshold` to `__init__`:

```python
class TransferEngine:
    def __init__(
        self,
        client: TelegramClient,
        db: TransferDB,
        tmp_dir: str = "/tmp/tg_transfer",
        retry_limit: int = 3,
        progress_interval: int = 20,
        media_db: MediaDB = None,
        phash_threshold: int = 10,
    ):
        self.client = client
        self.db = db
        self.tmp_dir = tmp_dir
        self.retry_limit = retry_limit
        self.progress_interval = progress_interval
        self.media_db = media_db
        self.phash_threshold = phash_threshold
```

Replace `_transfer_media` method:

```python
    async def _transfer_media(self, target_entity, message, target_chat: str = "",
                               source_chat: str = "", job_id: str = None) -> dict:
        """Download and re-upload a single media message.
        Returns: {"ok": bool, "dedup": bool, "similar": list | None}
        """
        job_dir = os.path.join(self.tmp_dir, str(message.id))
        os.makedirs(job_dir, exist_ok=True)

        try:
            path = await self.client.download_media(message, file=job_dir)
            if not path:
                return {"ok": False, "dedup": False, "similar": None}

            # Compute hashes
            sha256 = compute_sha256(path)
            file_type = self._detect_file_type(message)
            phash = None
            if file_type == "video":
                phash = await compute_phash_video(path, job_dir)
            elif file_type == "photo":
                phash = compute_phash(path)

            # Check dedup if media_db available
            if self.media_db:
                existing = await self.media_db.find_by_sha256(sha256, target_chat)
                if existing:
                    return {"ok": True, "dedup": True, "similar": None}

                # Check pHash similarity
                if phash:
                    all_phashes = await self.media_db.get_all_phashes()
                    similar = []
                    for row in all_phashes:
                        dist = hamming_distance(phash, row["phash"])
                        if dist <= self.phash_threshold:
                            similar.append({**row, "distance": dist})
                    if similar:
                        return {"ok": False, "dedup": False, "similar": similar}

                # Insert pending media record
                caption = message.text or ""
                file_size = os.path.getsize(path) if os.path.exists(path) else None
                media_id = await self.media_db.insert_media(
                    sha256=sha256, phash=phash, file_type=file_type,
                    file_size=file_size, caption=caption,
                    source_chat=source_chat, source_msg_id=message.id,
                    target_chat=target_chat, job_id=job_id,
                )

            # Upload
            result = await self.client.send_file(
                target_entity, path, caption=message.text
            )

            # Record success
            if self.media_db and result:
                target_msg_id = result.id if hasattr(result, 'id') else None
                if target_msg_id:
                    await self.media_db.mark_uploaded(media_id, target_msg_id)
                    tags = extract_tags(message.text)
                    if tags:
                        await self.media_db.add_tags(media_id, tags)

            return {"ok": True, "dedup": False, "similar": None}
        except Exception as e:
            # Clean up pending media on failure
            if self.media_db:
                try:
                    await self.media_db.delete_media(media_id)
                except Exception:
                    pass
            raise
        finally:
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)

    @staticmethod
    def _detect_file_type(message) -> str:
        if message.photo:
            return "photo"
        if message.video:
            return "video"
        return "document"
```

Update `transfer_single` to use the new return format:

```python
    async def transfer_single(self, source_entity, target_entity, message,
                               target_chat: str = "", source_chat: str = "",
                               job_id: str = None) -> dict:
        """Transfer a single message. Returns {"ok": bool, "dedup": bool, "similar": list | None}."""
        if message.media and not self.should_skip(message):
            return await self._transfer_media(
                target_entity, message, target_chat=target_chat,
                source_chat=source_chat, job_id=job_id,
            )
        elif message.text and not message.media:
            await self.client.send_message(target_entity, message.text)
            return {"ok": True, "dedup": False, "similar": None}
        elif self.should_skip(message):
            return {"ok": False, "dedup": False, "similar": None}
        return {"ok": True, "dedup": False, "similar": None}
```

- [ ] **Step 3: Update __main__.py — add search, stats, liveness, dashboard**

Add imports at the top of `agents/tg_transfer/__main__.py`:

```python
from agents.tg_transfer.media_db import MediaDB
from agents.tg_transfer.search import format_search_results, format_similar_results
from agents.tg_transfer.hasher import compute_phash, hamming_distance, PHASH_AVAILABLE
from agents.tg_transfer.liveness_checker import run_liveness_loop
from agents.tg_transfer.dashboard import dashboard_handler
```

In `_init_services`, after creating `self.engine`, add:

```python
        # Media DB
        self.media_db = MediaDB(os.path.join(data_dir, "transfer.db"))
        await self.media_db.init()

        # Pass media_db to engine
        self.engine.media_db = self.media_db
        self.engine.phash_threshold = settings.get("phash_threshold", 10)

        # Start liveness checker
        interval = settings.get("liveness_check_interval", 24)
        asyncio.create_task(run_liveness_loop(self.tg_client, self.media_db, interval))
```

Add `_search_state` dict in `__init__`:

```python
        self._search_state: dict[str, dict] = {}  # task_id → {keyword, page}
```

Override `create_app` to add dashboard route:

```python
    def create_app(self) -> web.Application:
        app = super().create_app()
        app.router.add_get("/dashboard", dashboard_handler)
        return app

    async def run(self) -> None:
        await self._init_services()
        # Store media_db in app for dashboard handler
        app = self.create_app()
        app["media_db"] = self.media_db
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        actual_port = site._server.sockets[0].getsockname()[1]
        print(f"Agent '{self.name}' running on port {actual_port}")
        await self.register(actual_port)
        await self._heartbeat_loop(actual_port)
```

In `_dispatch`, add new intent handlers before the batch fallback:

```python
        if intent == "stats":
            return await self._handle_stats()

        if intent == "page":
            return await self._handle_page(task)

        if intent == "search":
            return await self._handle_search(task)
```

Add new handler methods:

```python
    async def _handle_search(self, task: TaskRequest) -> AgentResult:
        """Handle keyword or image search."""
        content = task.content
        metadata = {}
        if task.conversation_history:
            metadata = task.conversation_history[-1].get("metadata", {})

        # Check if user sent an image (for image search)
        if metadata.get("has_photo"):
            return await self._handle_image_search(task, metadata)

        # Keyword search — strip search trigger words
        import re
        keyword = re.sub(r"(搜尋|查詢|search|找)\s*", "", content, flags=re.IGNORECASE).strip()
        if not keyword:
            return AgentResult(status=TaskStatus.NEED_INPUT, message="請輸入搜尋關鍵字")

        page_size = self.config.get("settings", {}).get("search_page_size", 10)
        results, total = await self.media_db.search_keyword(keyword, page=1, page_size=page_size)
        text = format_search_results(results, total, page=1, page_size=page_size)

        if total > page_size:
            self._search_state[task.task_id] = {"keyword": keyword, "page": 1}

        return AgentResult(status=TaskStatus.DONE, message=text)

    async def _handle_image_search(self, task: TaskRequest, metadata: dict) -> AgentResult:
        """Handle image-based similar search."""
        if not PHASH_AVAILABLE:
            return AgentResult(status=TaskStatus.DONE, message="pHash 不可用，僅支援關鍵字搜尋")

        photo_path = metadata.get("photo_path")
        if not photo_path:
            return AgentResult(status=TaskStatus.DONE, message="無法取得圖片")

        phash = compute_phash(photo_path)
        if not phash:
            return AgentResult(status=TaskStatus.DONE, message="無法計算圖片 hash")

        threshold = self.config.get("settings", {}).get("phash_threshold", 10)
        all_phashes = await self.media_db.get_all_phashes()
        similar = []
        for row in all_phashes:
            dist = hamming_distance(phash, row["phash"])
            if dist <= threshold:
                similar.append({**row, "distance": dist})
        similar.sort(key=lambda x: x["distance"])

        text = format_similar_results(similar)
        return AgentResult(status=TaskStatus.DONE, message=text)

    async def _handle_page(self, task: TaskRequest) -> AgentResult:
        """Handle pagination for search results."""
        state = self._search_state.get(task.task_id)
        if not state:
            return AgentResult(status=TaskStatus.DONE, message="沒有進行中的搜尋")

        content = task.content.strip().lower()
        page_size = self.config.get("settings", {}).get("search_page_size", 10)

        if "下一頁" in content or "next" in content:
            state["page"] += 1
        elif "上一頁" in content or "prev" in content:
            state["page"] = max(1, state["page"] - 1)

        results, total = await self.media_db.search_keyword(
            state["keyword"], page=state["page"], page_size=page_size
        )
        text = format_search_results(results, total, page=state["page"], page_size=page_size)
        return AgentResult(status=TaskStatus.DONE, message=text)

    async def _handle_stats(self) -> AgentResult:
        """Return media stats as text."""
        stats = await self.media_db.get_stats()
        lines = [
            f"儲存媒體：{stats['total_media']} 筆",
            f"標籤總數：{stats['total_tags']} 個",
        ]
        if stats["tag_counts"]:
            lines.append("\n標籤統計：")
            for name, count in stats["tag_counts"][:20]:
                lines.append(f"  #{name} — {count} 筆")
        return AgentResult(status=TaskStatus.DONE, message="\n".join(lines))
```

Update `_handle_single` to pass new params to `transfer_single` and handle similar results:

Replace the single transfer call in `_handle_single` (the `else` branch for non-album):

```python
            result = await self.engine.transfer_single(
                source_entity, target_entity, msg,
                target_chat=target_chat, source_chat=str(chat_id), job_id=None,
            )
            if result["similar"]:
                text = format_similar_results(result["similar"])
                return AgentResult(status=TaskStatus.NEED_INPUT, message=text)
            if result["dedup"]:
                return AgentResult(status=TaskStatus.DONE, message="已存在相同媒體，跳過")
            ok = result["ok"]
            count = 1
```

- [ ] **Step 4: Run all tests**

Run: `python3 -m pytest tests/test_parser.py tests/test_hasher.py tests/test_tag_extractor.py tests/test_media_db.py tests/test_search.py tests/test_liveness.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/transfer_engine.py agents/tg_transfer/__main__.py agents/tg_transfer/parser.py
git commit -m "feat(tg-transfer): integrate hash dedup, search, tags, stats, liveness into agent"
```
