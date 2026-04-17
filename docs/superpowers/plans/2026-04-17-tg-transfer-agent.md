# TG Transfer Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Telegram resource migration agent that copies messages (text, images, videos, files) between groups with resume capability and deduplication.

**Architecture:** Agent self-contains a Telethon client for direct TG operations. SQLite tracks job/message state for resume and dedup. AI (Gemini Flash) only parses natural language batch commands and formats completion reports — all transfer logic is pure code.

**Tech Stack:** Python 3.12, Telethon, aiosqlite, aiohttp (BaseAgent), Gemini CLI (intent parsing)

---

## File Structure

```
agents/tg_transfer/
├── agent.yaml          # Route patterns, priority, default settings
├── __init__.py         # Empty
├── __main__.py         # TGTransferAgent(BaseAgent), handle_task dispatch
├── parser.py           # TG link regex, forward detection, intent classification
├── chat_resolver.py    # Name/invite link/message link → Telethon entity
├── db.py               # SQLite: jobs, job_messages, config tables
├── transfer_engine.py  # Download/upload/album/progress/retry logic
├── tg_client.py        # Telethon client init + session management
├── Dockerfile          # Python 3.12-alpine + telethon + aiosqlite
tests/
├── test_parser.py      # Link parsing, forward detection tests
├── test_db.py          # DB operations, resume, dedup tests
├── test_transfer_engine.py  # Transfer logic with mocked Telethon
├── test_tg_transfer_agent.py  # handle_task dispatch tests
```

---

### Task 1: Project Scaffold + agent.yaml

**Files:**
- Create: `agents/tg_transfer/__init__.py`
- Create: `agents/tg_transfer/agent.yaml`

- [ ] **Step 1: Create empty __init__.py**

```python
# agents/tg_transfer/__init__.py
```

- [ ] **Step 2: Create agent.yaml**

```yaml
name: tg-transfer-agent
description: "Telegram 群組資源搬移工具：支援單則/批量搬移、斷點續傳、跨 Job 去重"
priority: 3
route_patterns:
  - "搬移|轉存|複製群組|搬到|copy to|transfer|備份群組"
sandbox:
  allowed_dirs: []
  writable: false
settings:
  default_target_chat: ""
  retry_limit: 3
  progress_interval: 20
  telethon_session: "tg_transfer"
```

- [ ] **Step 3: Commit**

```bash
git add agents/tg_transfer/__init__.py agents/tg_transfer/agent.yaml
git commit -m "feat(tg-transfer): scaffold agent directory with agent.yaml"
```

---

### Task 2: Parser — Link Regex & Forward Detection

**Files:**
- Create: `agents/tg_transfer/parser.py`
- Create: `tests/test_parser.py`

- [ ] **Step 1: Write failing tests for parser**

```python
# tests/test_parser.py
import pytest
from agents.tg_transfer.parser import (
    parse_tg_link,
    detect_forward,
    classify_intent,
    ParsedLink,
)


class TestParseTgLink:
    def test_public_message_link(self):
        result = parse_tg_link("https://t.me/channel_name/123")
        assert result == ParsedLink(chat="channel_name", message_id=123, is_private=False)

    def test_private_message_link(self):
        result = parse_tg_link("https://t.me/c/1234567890/456")
        assert result == ParsedLink(chat=1234567890, message_id=456, is_private=True)

    def test_message_link_in_text(self):
        text = "幫我搬這個 https://t.me/channel_name/789 到備份群"
        result = parse_tg_link(text)
        assert result == ParsedLink(chat="channel_name", message_id=789, is_private=False)

    def test_no_link(self):
        result = parse_tg_link("這只是一段普通文字")
        assert result is None

    def test_invite_link_not_message(self):
        result = parse_tg_link("https://t.me/+AbCdEfG")
        assert result is None


class TestDetectForward:
    def test_forwarded_message(self):
        content = "這是一段訊息"
        metadata = {"forward_chat_id": -1001234567890, "forward_message_id": 42}
        result = detect_forward(content, metadata)
        assert result == ParsedLink(chat=-1001234567890, message_id=42, is_private=True)

    def test_not_forwarded(self):
        result = detect_forward("普通訊息", {})
        assert result is None


class TestClassifyIntent:
    def test_link_intent(self):
        assert classify_intent("https://t.me/ch/123") == "single_transfer"

    def test_config_intent_default_target(self):
        assert classify_intent("預設目標改成 @my_backup") == "config"

    def test_config_intent_set_target(self):
        assert classify_intent("設定目標群組 @new_channel") == "config"

    def test_batch_intent(self):
        assert classify_intent("把 @old_channel 的東西搬到 @new_channel") == "batch"

    def test_link_with_surrounding_text(self):
        assert classify_intent("幫我搬 https://t.me/c/123/456") == "single_transfer"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_parser.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement parser.py**

```python
# agents/tg_transfer/parser.py
import re
from dataclasses import dataclass
from typing import Optional

# https://t.me/channel_name/123 (public)
_PUBLIC_MSG_RE = re.compile(r"https?://t\.me/([A-Za-z_]\w+)/(\d+)")
# https://t.me/c/1234567890/456 (private)
_PRIVATE_MSG_RE = re.compile(r"https?://t\.me/c/(\d+)/(\d+)")
# Config keywords
_CONFIG_RE = re.compile(r"(預設目標|設定目標|default.?target)", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedLink:
    chat: int | str  # chat_id (int) or username (str)
    message_id: int
    is_private: bool


def parse_tg_link(text: str) -> Optional[ParsedLink]:
    """Extract a TG message link from text. Returns None if no message link found."""
    m = _PRIVATE_MSG_RE.search(text)
    if m:
        return ParsedLink(chat=int(m.group(1)), message_id=int(m.group(2)), is_private=True)
    m = _PUBLIC_MSG_RE.search(text)
    if m:
        return ParsedLink(chat=m.group(1), message_id=int(m.group(2)), is_private=False)
    return None


def detect_forward(content: str, metadata: dict) -> Optional[ParsedLink]:
    """Detect if a message is forwarded. Returns ParsedLink if forward info present."""
    chat_id = metadata.get("forward_chat_id")
    msg_id = metadata.get("forward_message_id")
    if chat_id is not None and msg_id is not None:
        return ParsedLink(chat=chat_id, message_id=msg_id, is_private=True)
    return None


def classify_intent(text: str) -> str:
    """Classify user message intent.
    Returns: 'single_transfer', 'config', or 'batch'.
    """
    if parse_tg_link(text) is not None:
        return "single_transfer"
    if _CONFIG_RE.search(text):
        return "config"
    return "batch"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_parser.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/parser.py tests/test_parser.py
git commit -m "feat(tg-transfer): add message link parser and intent classifier"
```

---

### Task 3: Database Layer

**Files:**
- Create: `agents/tg_transfer/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for db**

```python
# tests/test_db.py
import pytest
import asyncio
from agents.tg_transfer.db import TransferDB


@pytest.fixture
async def db(tmp_path):
    database = TransferDB(str(tmp_path / "test.db"))
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_create_job(db):
    job_id = await db.create_job(
        source_chat="@source",
        target_chat="@target",
        mode="batch",
        filter_type="count",
        filter_value='{"count": 50}',
    )
    job = await db.get_job(job_id)
    assert job["source_chat"] == "@source"
    assert job["target_chat"] == "@target"
    assert job["status"] == "pending"
    assert job["mode"] == "batch"


@pytest.mark.asyncio
async def test_add_messages_and_get_next_pending(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [100, 101, 102])
    msg = await db.get_next_pending(job_id)
    assert msg["message_id"] == 100


@pytest.mark.asyncio
async def test_mark_success(db):
    job_id = await db.create_job("@src", "@dst", "single")
    await db.add_messages(job_id, [200])
    await db.mark_message(job_id, 200, "success")
    msg = await db.get_next_pending(job_id)
    assert msg is None  # no more pending


@pytest.mark.asyncio
async def test_mark_failed_with_error(db):
    job_id = await db.create_job("@src", "@dst", "single")
    await db.add_messages(job_id, [300])
    await db.mark_message(job_id, 300, "failed", error="timeout")
    await db.increment_retry(job_id, 300)
    msg = await db.get_message(job_id, 300)
    assert msg["status"] == "failed"
    assert msg["retry_count"] == 1
    assert msg["error"] == "timeout"


@pytest.mark.asyncio
async def test_job_progress(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [1, 2, 3, 4, 5])
    await db.mark_message(job_id, 1, "success")
    await db.mark_message(job_id, 2, "success")
    await db.mark_message(job_id, 3, "skipped")
    progress = await db.get_progress(job_id)
    assert progress == {"total": 5, "success": 2, "failed": 0, "skipped": 1, "pending": 2}


@pytest.mark.asyncio
async def test_update_job_status(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.update_job_status(job_id, "running")
    job = await db.get_job(job_id)
    assert job["status"] == "running"


@pytest.mark.asyncio
async def test_set_auto_skip(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.set_auto_skip(job_id, True)
    job = await db.get_job(job_id)
    assert job["auto_skip"] == 1


@pytest.mark.asyncio
async def test_dedup_returns_existing_success_ids(db):
    job1 = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job1, [10, 11, 12])
    await db.mark_message(job1, 10, "success")
    await db.mark_message(job1, 11, "success")
    await db.mark_message(job1, 12, "failed")
    await db.update_job_status(job1, "completed")

    already_done = await db.get_transferred_message_ids("@src", "@dst")
    assert already_done == {10, 11}


@pytest.mark.asyncio
async def test_config_get_set(db):
    await db.set_config("default_target_chat", "@my_backup")
    val = await db.get_config("default_target_chat")
    assert val == "@my_backup"

    await db.set_config("default_target_chat", "@new_backup")
    val = await db.get_config("default_target_chat")
    assert val == "@new_backup"


@pytest.mark.asyncio
async def test_config_get_missing(db):
    val = await db.get_config("nonexistent")
    assert val is None


@pytest.mark.asyncio
async def test_get_running_jobs(db):
    job1 = await db.create_job("@a", "@b", "batch")
    job2 = await db.create_job("@c", "@d", "batch")
    await db.update_job_status(job1, "running")
    await db.update_job_status(job2, "completed")
    running = await db.get_running_jobs()
    assert len(running) == 1
    assert running[0]["job_id"] == job1


@pytest.mark.asyncio
async def test_add_messages_with_grouped_id(db):
    job_id = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job_id, [50, 51, 52], grouped_ids={50: 999, 51: 999})
    msgs = await db.get_grouped_messages(job_id, 999)
    assert len(msgs) == 2
    assert {m["message_id"] for m in msgs} == {50, 51}


@pytest.mark.asyncio
async def test_reset_message_to_pending(db):
    job_id = await db.create_job("@src", "@dst", "single")
    await db.add_messages(job_id, [400])
    await db.mark_message(job_id, 400, "failed", error="network")
    await db.reset_message(job_id, 400)
    msg = await db.get_message(job_id, 400)
    assert msg["status"] == "pending"
    assert msg["error"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_db.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement db.py**

```python
# agents/tg_transfer/db.py
import aiosqlite
import uuid
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    source_chat TEXT NOT NULL,
    target_chat TEXT NOT NULL,
    filter_type TEXT,
    filter_value TEXT,
    mode        TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',
    auto_skip   BOOLEAN DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_messages (
    job_id      TEXT NOT NULL,
    message_id  INTEGER NOT NULL,
    grouped_id  INTEGER,
    status      TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    error       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (job_id, message_id),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_messages_status
    ON job_messages(job_id, status);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class TransferDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # -- Jobs --

    async def create_job(
        self,
        source_chat: str,
        target_chat: str,
        mode: str,
        filter_type: str = None,
        filter_value: str = None,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        await self._db.execute(
            "INSERT INTO jobs (job_id, source_chat, target_chat, mode, filter_type, filter_value) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, source_chat, target_chat, mode, filter_type, filter_value),
        )
        await self._db.commit()
        return job_id

    async def get_job(self, job_id: str) -> Optional[dict]:
        async with self._db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_job_status(self, job_id: str, status: str):
        await self._db.execute(
            "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
            (status, job_id),
        )
        await self._db.commit()

    async def set_auto_skip(self, job_id: str, auto_skip: bool):
        await self._db.execute(
            "UPDATE jobs SET auto_skip = ? WHERE job_id = ?", (int(auto_skip), job_id)
        )
        await self._db.commit()

    async def get_running_jobs(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM jobs WHERE status = 'running'") as cur:
            return [dict(row) for row in await cur.fetchall()]

    # -- Messages --

    async def add_messages(
        self, job_id: str, message_ids: list[int], grouped_ids: dict[int, int] = None
    ):
        grouped_ids = grouped_ids or {}
        for msg_id in message_ids:
            gid = grouped_ids.get(msg_id)
            await self._db.execute(
                "INSERT OR IGNORE INTO job_messages (job_id, message_id, grouped_id) VALUES (?, ?, ?)",
                (job_id, msg_id, gid),
            )
        await self._db.commit()

    async def get_next_pending(self, job_id: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM job_messages WHERE job_id = ? AND status = 'pending' ORDER BY message_id ASC LIMIT 1",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_message(self, job_id: str, message_id: int) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM job_messages WHERE job_id = ? AND message_id = ?",
            (job_id, message_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def mark_message(self, job_id: str, message_id: int, status: str, error: str = None):
        await self._db.execute(
            "UPDATE job_messages SET status = ?, error = ? WHERE job_id = ? AND message_id = ?",
            (status, error, job_id, message_id),
        )
        await self._db.commit()

    async def increment_retry(self, job_id: str, message_id: int):
        await self._db.execute(
            "UPDATE job_messages SET retry_count = retry_count + 1 WHERE job_id = ? AND message_id = ?",
            (job_id, message_id),
        )
        await self._db.commit()

    async def reset_message(self, job_id: str, message_id: int):
        await self._db.execute(
            "UPDATE job_messages SET status = 'pending', error = NULL WHERE job_id = ? AND message_id = ?",
            (job_id, message_id),
        )
        await self._db.commit()

    async def get_grouped_messages(self, job_id: str, grouped_id: int) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM job_messages WHERE job_id = ? AND grouped_id = ? ORDER BY message_id ASC",
            (job_id, grouped_id),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def get_progress(self, job_id: str) -> dict:
        counts = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "pending": 0}
        async with self._db.execute(
            "SELECT status, COUNT(*) as cnt FROM job_messages WHERE job_id = ? GROUP BY status",
            (job_id,),
        ) as cur:
            async for row in cur:
                counts[row["status"]] = row["cnt"]
                counts["total"] += row["cnt"]
        return counts

    # -- Dedup --

    async def get_transferred_message_ids(self, source_chat: str, target_chat: str) -> set[int]:
        async with self._db.execute(
            "SELECT jm.message_id FROM job_messages jm "
            "JOIN jobs j ON jm.job_id = j.job_id "
            "WHERE j.source_chat = ? AND j.target_chat = ? AND jm.status = 'success'",
            (source_chat, target_chat),
        ) as cur:
            return {row["message_id"] for row in await cur.fetchall()}

    # -- Config --

    async def get_config(self, key: str) -> Optional[str]:
        async with self._db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def set_config(self, key: str, value: str):
        await self._db.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._db.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_db.py -v`
Expected: All PASS (requires `pip install aiosqlite` if not installed)

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/db.py tests/test_db.py
git commit -m "feat(tg-transfer): add SQLite database layer with job/message/config tables"
```

---

### Task 4: Telethon Client Wrapper

**Files:**
- Create: `agents/tg_transfer/tg_client.py`

- [ ] **Step 1: Implement tg_client.py**

```python
# agents/tg_transfer/tg_client.py
import os
import logging
from telethon import TelegramClient

logger = logging.getLogger(__name__)


async def create_client(session_path: str) -> TelegramClient:
    """Create and start a Telethon client.

    Requires env vars: TG_API_ID, TG_API_HASH.
    First run requires interactive login (phone + code).
    Subsequent runs use the persisted session file.
    """
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    logger.info("Telethon client connected")
    return client
```

- [ ] **Step 2: Commit**

```bash
git add agents/tg_transfer/tg_client.py
git commit -m "feat(tg-transfer): add Telethon client wrapper"
```

---

### Task 5: Chat Resolver

**Files:**
- Create: `agents/tg_transfer/chat_resolver.py`

- [ ] **Step 1: Implement chat_resolver.py**

```python
# agents/tg_transfer/chat_resolver.py
import re
import logging
from telethon import TelegramClient
from telethon.tl.functions.messages import CheckChatInviteRequest

logger = logging.getLogger(__name__)

_INVITE_RE = re.compile(r"https?://t\.me/\+([A-Za-z0-9_-]+)")
_USERNAME_RE = re.compile(r"@([A-Za-z_]\w+)")


async def resolve_chat(client: TelegramClient, identifier: str):
    """Resolve a chat identifier to a Telethon entity.

    Supports:
    - @username
    - https://t.me/+INVITE_HASH (invite link)
    - username (plain text)
    - integer chat_id
    """
    identifier = identifier.strip()

    # Invite link
    m = _INVITE_RE.search(identifier)
    if m:
        invite_hash = m.group(1)
        invite = await client(CheckChatInviteRequest(invite_hash))
        if hasattr(invite, "chat"):
            return invite.chat
        # ChatInviteAlready means we're already a member
        if hasattr(invite, "already_joined"):
            # Need to join first or get entity differently
            updates = await client(
                __import__("telethon.tl.functions.messages", fromlist=["ImportChatInviteRequest"])
                .ImportChatInviteRequest(invite_hash)
            )
            return updates.chats[0]
        raise ValueError(f"Cannot resolve invite link: {identifier}")

    # @username
    m = _USERNAME_RE.match(identifier)
    if m:
        return await client.get_entity(m.group(1))

    # Integer chat_id
    try:
        chat_id = int(identifier)
        return await client.get_entity(chat_id)
    except (ValueError, TypeError):
        pass

    # Plain username
    return await client.get_entity(identifier)
```

- [ ] **Step 2: Commit**

```bash
git add agents/tg_transfer/chat_resolver.py
git commit -m "feat(tg-transfer): add chat resolver for usernames, invite links, and chat IDs"
```

---

### Task 6: Transfer Engine

**Files:**
- Create: `agents/tg_transfer/transfer_engine.py`
- Create: `tests/test_transfer_engine.py`

- [ ] **Step 1: Write failing tests for transfer engine**

```python
# tests/test_transfer_engine.py
import pytest
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from agents.tg_transfer.transfer_engine import TransferEngine
from agents.tg_transfer.db import TransferDB


@pytest.fixture
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
    assert result is True
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_transfer_engine.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement transfer_engine.py**

```python
# agents/tg_transfer/transfer_engine.py
import os
import shutil
import logging
import asyncio
from typing import Callable, Optional, Any
from telethon import TelegramClient
from agents.tg_transfer.db import TransferDB

logger = logging.getLogger(__name__)


class TransferEngine:
    def __init__(
        self,
        client: TelegramClient,
        db: TransferDB,
        tmp_dir: str = "/tmp/tg_transfer",
        retry_limit: int = 3,
        progress_interval: int = 20,
    ):
        self.client = client
        self.db = db
        self.tmp_dir = tmp_dir
        self.retry_limit = retry_limit
        self.progress_interval = progress_interval

    def should_skip(self, message) -> bool:
        """Check if message type should be skipped (sticker, poll, voice)."""
        if message.sticker:
            return True
        if message.poll:
            return True
        if message.voice:
            return True
        return False

    async def transfer_single(self, source_entity, target_entity, message) -> bool:
        """Transfer a single message (text or media) to target chat."""
        if message.media and not self.should_skip(message):
            return await self._transfer_media(target_entity, message)
        elif message.text and not message.media:
            await self.client.send_message(target_entity, message.text)
            return True
        elif self.should_skip(message):
            return False  # caller marks as skipped
        return True

    async def transfer_album(self, target_entity, messages: list) -> bool:
        """Transfer a media group (album) as a single album."""
        job_dir = os.path.join(self.tmp_dir, "album")
        os.makedirs(job_dir, exist_ok=True)

        files = []
        caption = None
        try:
            for msg in messages:
                path = await self.client.download_media(msg, file=job_dir)
                if path:
                    files.append(path)
                if msg.text and not caption:
                    caption = msg.text

            if files:
                await self.client.send_file(
                    target_entity, files, caption=caption
                )
                return True
            return False
        finally:
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)

    async def _transfer_media(self, target_entity, message) -> bool:
        """Download and re-upload a single media message."""
        job_dir = os.path.join(self.tmp_dir, str(message.id))
        os.makedirs(job_dir, exist_ok=True)

        try:
            path = await self.client.download_media(message, file=job_dir)
            if path:
                await self.client.send_file(
                    target_entity, path, caption=message.text
                )
                return True
            return False
        finally:
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)

    async def run_batch(
        self,
        job_id: str,
        source_entity,
        target_entity,
        report_fn: Callable[[str], Any],
    ) -> str:
        """Run a batch transfer job. Returns final status: 'completed', 'paused', or 'failed'.

        report_fn: async callback to send progress/error messages to user.
        """
        await self.db.update_job_status(job_id, "running")
        job = await self.db.get_job(job_id)
        processed = 0

        while True:
            msg_row = await self.db.get_next_pending(job_id)
            if msg_row is None:
                break  # all done

            message_id = msg_row["message_id"]
            grouped_id = msg_row["grouped_id"]

            try:
                # Handle album group
                if grouped_id:
                    group_rows = await self.db.get_grouped_messages(job_id, grouped_id)
                    # Only process when we hit the first pending in the group
                    if group_rows[0]["message_id"] != message_id:
                        # Already handled as part of group
                        await self.db.mark_message(job_id, message_id, "success")
                        processed += 1
                        continue

                    messages = []
                    for gr in group_rows:
                        msg = await self.client.get_messages(source_entity, ids=gr["message_id"])
                        if msg:
                            messages.append(msg)

                    if messages and not self.should_skip(messages[0]):
                        ok = await self.transfer_album(target_entity, messages)
                        status = "success" if ok else "failed"
                    elif messages and self.should_skip(messages[0]):
                        status = "skipped"
                    else:
                        status = "failed"

                    for gr in group_rows:
                        await self.db.mark_message(job_id, gr["message_id"], status)
                    processed += len(group_rows)
                else:
                    # Single message
                    msg = await self.client.get_messages(source_entity, ids=message_id)
                    if msg is None:
                        await self.db.mark_message(job_id, message_id, "failed", error="message deleted")
                        processed += 1
                        continue

                    if self.should_skip(msg):
                        await self.db.mark_message(job_id, message_id, "skipped")
                    else:
                        ok = await self.transfer_single(source_entity, target_entity, msg)
                        await self.db.mark_message(
                            job_id, message_id, "success" if ok else "failed"
                        )
                    processed += 1

            except Exception as e:
                logger.error(f"Transfer error for msg {message_id}: {e}")
                await self.db.increment_retry(job_id, message_id)
                msg_row = await self.db.get_message(job_id, message_id)

                if msg_row["retry_count"] >= self.retry_limit:
                    # Check auto_skip
                    job = await self.db.get_job(job_id)
                    if job["auto_skip"]:
                        await self.db.mark_message(job_id, message_id, "skipped", error=str(e))
                        processed += 1
                        continue

                    # Pause and ask user
                    await self.db.mark_message(job_id, message_id, "failed", error=str(e))
                    await self.db.update_job_status(job_id, "paused")
                    progress = await self.db.get_progress(job_id)
                    await report_fn(
                        f"訊息 #{message_id} 失敗（已重試 {self.retry_limit} 次）\n"
                        f"錯誤：{e}\n"
                        f"進度：{progress['success']}/{progress['total']}\n\n"
                        f"請選擇：重試 / 跳過 / 一律跳過"
                    )
                    return "paused"
                else:
                    # Reset to pending for next retry
                    await self.db.reset_message(job_id, message_id)
                    continue

            # Progress report
            if processed > 0 and processed % self.progress_interval == 0:
                progress = await self.db.get_progress(job_id)
                await report_fn(
                    f"進度：{progress['success'] + progress['skipped']}/{progress['total']} "
                    f"（成功 {progress['success']}，跳過 {progress['skipped']}）"
                )

        # Completed
        await self.db.update_job_status(job_id, "completed")
        return "completed"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_transfer_engine.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/transfer_engine.py tests/test_transfer_engine.py
git commit -m "feat(tg-transfer): add transfer engine with batch/single/album support"
```

---

### Task 7: Agent Main — handle_task Dispatch

**Files:**
- Create: `agents/tg_transfer/__main__.py`
- Create: `tests/test_tg_transfer_agent.py`

- [ ] **Step 1: Write failing tests for agent dispatch**

```python
# tests/test_tg_transfer_agent.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.models import TaskRequest, TaskStatus


class TestHandleTaskDispatch:
    """Test that handle_task routes to correct handler based on input."""

    @pytest.mark.asyncio
    async def test_link_triggers_single_transfer(self):
        from agents.tg_transfer.__main__ import TGTransferAgent

        with patch.object(TGTransferAgent, "__init__", lambda self, **kw: None):
            agent = TGTransferAgent.__new__(TGTransferAgent)
            agent.db = AsyncMock()
            agent.db.get_config = AsyncMock(return_value="@backup")
            agent.tg_client = AsyncMock()
            agent.engine = AsyncMock()
            agent.engine.transfer_single = AsyncMock(return_value=True)
            agent.engine.should_skip = MagicMock(return_value=False)
            agent.config = {"settings": {"retry_limit": 3, "progress_interval": 20}}
            agent._pending_jobs = {}

            task = TaskRequest(task_id="t1", content="https://t.me/channel/123")

            with patch("agents.tg_transfer.__main__.resolve_chat", new_callable=AsyncMock) as mock_resolve:
                mock_resolve.return_value = MagicMock()
                msg = MagicMock()
                msg.text = "hello"
                msg.media = None
                msg.grouped_id = None
                agent.tg_client.get_messages = AsyncMock(return_value=msg)
                result = await agent.handle_task(task)

            assert result.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_config_update(self):
        from agents.tg_transfer.__main__ import TGTransferAgent

        with patch.object(TGTransferAgent, "__init__", lambda self, **kw: None):
            agent = TGTransferAgent.__new__(TGTransferAgent)
            agent.db = AsyncMock()
            agent.db.set_config = AsyncMock()
            agent.tg_client = AsyncMock()
            agent.engine = AsyncMock()
            agent.config = {"settings": {"retry_limit": 3, "progress_interval": 20}}
            agent._pending_jobs = {}

            task = TaskRequest(task_id="t2", content="預設目標改成 @my_backup")
            result = await agent.handle_task(task)

            assert result.status == TaskStatus.DONE
            agent.db.set_config.assert_called_once_with("default_target_chat", "@my_backup")

    @pytest.mark.asyncio
    async def test_forward_triggers_transfer(self):
        from agents.tg_transfer.__main__ import TGTransferAgent

        with patch.object(TGTransferAgent, "__init__", lambda self, **kw: None):
            agent = TGTransferAgent.__new__(TGTransferAgent)
            agent.db = AsyncMock()
            agent.db.get_config = AsyncMock(return_value="@backup")
            agent.tg_client = AsyncMock()
            agent.engine = AsyncMock()
            agent.engine.transfer_single = AsyncMock(return_value=True)
            agent.engine.should_skip = MagicMock(return_value=False)
            agent.config = {"settings": {"retry_limit": 3, "progress_interval": 20}}
            agent._pending_jobs = {}

            task = TaskRequest(
                task_id="t3",
                content="轉發的訊息內容",
                conversation_history=[{
                    "role": "user",
                    "content": "轉發的訊息內容",
                    "metadata": {"forward_chat_id": -1001234567890, "forward_message_id": 42},
                }],
            )

            with patch("agents.tg_transfer.__main__.resolve_chat", new_callable=AsyncMock) as mock_resolve:
                mock_resolve.return_value = MagicMock()
                msg = MagicMock()
                msg.text = "hello"
                msg.media = None
                msg.grouped_id = None
                agent.tg_client.get_messages = AsyncMock(return_value=msg)
                result = await agent.handle_task(task)

            assert result.status == TaskStatus.DONE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_tg_transfer_agent.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement __main__.py**

```python
# agents/tg_transfer/__main__.py
import asyncio
import os
import re
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus
from agents.tg_transfer.parser import parse_tg_link, detect_forward, classify_intent
from agents.tg_transfer.chat_resolver import resolve_chat
from agents.tg_transfer.db import TransferDB
from agents.tg_transfer.transfer_engine import TransferEngine
from agents.tg_transfer.tg_client import create_client

logger = logging.getLogger(__name__)

_TARGET_RE = re.compile(r"(?:改成|設定為?|set\s+to)\s*(@\w+|https?://t\.me/\S+)", re.IGNORECASE)


class TGTransferAgent(BaseAgent):
    def __init__(self, hub_url: str, port: int = 0):
        agent_dir = os.path.dirname(os.path.abspath(__file__))
        super().__init__(agent_dir=agent_dir, hub_url=hub_url, port=port)
        self.db: TransferDB = None
        self.tg_client = None
        self.engine: TransferEngine = None
        self._pending_jobs: dict[str, str] = {}  # task_id → job_id

    async def _init_services(self):
        data_dir = os.environ.get("DATA_DIR", "/data/tg_transfer")
        os.makedirs(data_dir, exist_ok=True)

        self.db = TransferDB(os.path.join(data_dir, "transfer.db"))
        await self.db.init()

        # Load default_target_chat from yaml if not in DB yet
        settings = self.config.get("settings", {})
        yaml_target = settings.get("default_target_chat", "")
        if yaml_target and not await self.db.get_config("default_target_chat"):
            await self.db.set_config("default_target_chat", yaml_target)

        session_name = settings.get("telethon_session", "tg_transfer")
        session_path = os.path.join(data_dir, session_name)
        self.tg_client = await create_client(session_path)

        self.engine = TransferEngine(
            client=self.tg_client,
            db=self.db,
            tmp_dir=os.path.join(data_dir, "tmp"),
            retry_limit=settings.get("retry_limit", 3),
            progress_interval=settings.get("progress_interval", 20),
        )

        # Resume interrupted jobs
        running_jobs = await self.db.get_running_jobs()
        for job in running_jobs:
            logger.info(f"Found interrupted job {job['job_id']}, will resume on next dispatch")

    async def handle_task(self, task: TaskRequest) -> AgentResult:
        try:
            return await self._dispatch(task)
        except Exception as e:
            logger.error(f"handle_task error: {e}", exc_info=True)
            return AgentResult(status=TaskStatus.ERROR, message=f"執行失敗：{e}")

    async def _dispatch(self, task: TaskRequest) -> AgentResult:
        content = task.content
        metadata = {}
        if task.conversation_history:
            metadata = task.conversation_history[-1].get("metadata", {})

        # Check if this is a response to a paused job
        if task.task_id in self._pending_jobs:
            return await self._handle_paused_response(task)

        # Check for forwarded message
        fwd = detect_forward(content, metadata)
        if fwd:
            return await self._handle_single(task, fwd.chat, fwd.message_id)

        # Classify intent
        intent = classify_intent(content)

        if intent == "single_transfer":
            link = parse_tg_link(content)
            return await self._handle_single(task, link.chat, link.message_id)

        if intent == "config":
            return await self._handle_config(content)

        # Batch — use AI to parse (for now, simple extraction)
        return await self._handle_batch_request(task)

    async def _handle_single(self, task: TaskRequest, chat_id, message_id: int) -> AgentResult:
        target_chat = await self.db.get_config("default_target_chat")
        if not target_chat:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="尚未設定預設目標群組。請先設定：「預設目標改成 @群組名稱」",
            )

        source_entity = await self.tg_client.get_entity(chat_id)
        target_entity = await resolve_chat(self.tg_client, target_chat)

        msg = await self.tg_client.get_messages(source_entity, ids=message_id)
        if msg is None:
            return AgentResult(status=TaskStatus.ERROR, message="找不到該訊息，可能已被刪除")

        # Check for album
        if msg.grouped_id:
            # Get all messages in the album
            # Search nearby messages with same grouped_id
            nearby = await self.tg_client.get_messages(source_entity, ids=range(message_id - 10, message_id + 10))
            album_msgs = [m for m in nearby if m and m.grouped_id == msg.grouped_id]
            album_msgs.sort(key=lambda m: m.id)

            if self.engine.should_skip(album_msgs[0]):
                return AgentResult(status=TaskStatus.DONE, message="已跳過（不支援的訊息類型）")

            ok = await self.engine.transfer_album(target_entity, album_msgs)
            count = len(album_msgs)
        else:
            if self.engine.should_skip(msg):
                return AgentResult(status=TaskStatus.DONE, message="已跳過（不支援的訊息類型）")
            ok = await self.engine.transfer_single(source_entity, target_entity, msg)
            count = 1

        if ok:
            return AgentResult(status=TaskStatus.DONE, message=f"已轉存 {count} 則訊息到 {target_chat}")
        return AgentResult(status=TaskStatus.ERROR, message="轉存失敗")

    async def _handle_config(self, content: str) -> AgentResult:
        m = _TARGET_RE.search(content)
        if m:
            target = m.group(1)
            await self.db.set_config("default_target_chat", target)
            return AgentResult(status=TaskStatus.DONE, message=f"預設目標已設為 {target}")
        return AgentResult(
            status=TaskStatus.NEED_INPUT,
            message="請告訴我目標群組，例如：「預設目標改成 @channel_name」",
        )

    async def _handle_batch_request(self, task: TaskRequest) -> AgentResult:
        """Parse batch command with AI, return estimate for confirmation."""
        # Use Gemini Flash to parse natural language
        content = task.content
        parsed = await self._ai_parse_batch(content)
        if not parsed:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="我沒有理解你的搬移指令，可以再說一次嗎？\n"
                        "例如：「把 @source_channel 的內容搬到 @target」\n"
                        "或：「搬移 @source 最近 100 則到 @target」",
            )

        source = parsed["source"]
        target = parsed.get("target") or await self.db.get_config("default_target_chat")
        if not target:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="請指定目標群組，或先設定預設目標",
            )

        source_entity = await resolve_chat(self.tg_client, source)
        filter_type = parsed.get("filter_type", "all")
        filter_value = parsed.get("filter_value")

        # Count messages
        count = await self._count_messages(source_entity, filter_type, filter_value)

        # Check dedup
        already_done = await self.db.get_transferred_message_ids(source, target)
        new_count = count - len(already_done) if already_done else count

        # Create job but don't start yet
        import json
        job_id = await self.db.create_job(
            source_chat=source,
            target_chat=target,
            mode="batch",
            filter_type=filter_type,
            filter_value=json.dumps(parsed.get("filter_value_raw")) if parsed.get("filter_value_raw") else None,
        )
        self._pending_jobs[task.task_id] = job_id

        dedup_note = f"（其中 {len(already_done)} 則已搬過，將跳過）" if already_done else ""
        return AgentResult(
            status=TaskStatus.NEED_INPUT,
            message=f"來源：{source}\n目標：{target}\n"
                    f"符合條件的訊息：約 {count} 則{dedup_note}\n"
                    f"預計搬移：{new_count} 則\n\n確認執行？（是/否）",
        )

    async def _handle_paused_response(self, task: TaskRequest) -> AgentResult:
        """Handle user response to a paused job (retry/skip/auto-skip) or batch confirmation."""
        job_id = self._pending_jobs[task.task_id]
        job = await self.db.get_job(job_id)
        content = task.content.strip().lower()

        if job["status"] == "pending":
            # This is a batch confirmation
            if content in ("是", "yes", "y", "確認", "ok"):
                return await self._start_batch(task.task_id, job_id, job)
            else:
                del self._pending_jobs[task.task_id]
                await self.db.update_job_status(job_id, "failed")
                return AgentResult(status=TaskStatus.DONE, message="已取消")

        if job["status"] == "paused":
            if content in ("重試", "retry"):
                # Find the failed message and reset it
                progress = await self.db.get_progress(job_id)
                return await self._resume_batch(task.task_id, job_id, job)
            elif content in ("跳過", "skip"):
                # Mark current failed as skipped, continue
                await self._skip_current_failed(job_id)
                return await self._resume_batch(task.task_id, job_id, job)
            elif content in ("一律跳過", "skip all", "auto skip"):
                await self.db.set_auto_skip(job_id, True)
                await self._skip_current_failed(job_id)
                return await self._resume_batch(task.task_id, job_id, job)

        return AgentResult(status=TaskStatus.NEED_INPUT, message="請選擇：重試 / 跳過 / 一律跳過")

    async def _skip_current_failed(self, job_id: str):
        """Find failed messages in job and mark as skipped."""
        # Get all failed messages
        async with self.db._db.execute(
            "SELECT message_id FROM job_messages WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            await self.db.mark_message(job_id, row["message_id"], "skipped")

    async def _start_batch(self, task_id: str, job_id: str, job: dict) -> AgentResult:
        """Populate job_messages and start batch transfer."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])

        # Collect message IDs
        import json
        filter_type = job["filter_type"] or "all"
        filter_value = json.loads(job["filter_value"]) if job["filter_value"] else None
        messages = await self._collect_messages(source_entity, filter_type, filter_value)

        # Dedup
        already_done = await self.db.get_transferred_message_ids(job["source_chat"], job["target_chat"])
        grouped_ids = {}
        msg_ids = []
        for msg in messages:
            if msg.id in already_done:
                continue
            msg_ids.append(msg.id)
            if msg.grouped_id:
                grouped_ids[msg.id] = msg.grouped_id

        await self.db.add_messages(job_id, msg_ids, grouped_ids)

        async def report_fn(text):
            # This will be sent back via Hub as NEED_INPUT
            pass  # Progress is tracked, reported via next handle_task call

        status = await self.engine.run_batch(job_id, source_entity, target_entity, report_fn)

        if status == "paused":
            # Job paused due to failure, keep in pending_jobs
            progress = await self.db.get_progress(job_id)
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message=f"搬移暫停\n"
                        f"進度：{progress['success']}/{progress['total']}\n"
                        f"請選擇：重試 / 跳過 / 一律跳過",
            )

        del self._pending_jobs[task_id]
        progress = await self.db.get_progress(job_id)
        return AgentResult(
            status=TaskStatus.DONE,
            message=f"搬移完成\n"
                    f"來源：{job['source_chat']}\n"
                    f"目標：{job['target_chat']}\n"
                    f"成功：{progress['success']} 則\n"
                    f"跳過：{progress['skipped']} 則\n"
                    f"失敗：{progress['failed']} 則",
        )

    async def _resume_batch(self, task_id: str, job_id: str, job: dict) -> AgentResult:
        """Resume a paused batch job."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])

        async def report_fn(text):
            pass

        status = await self.engine.run_batch(job_id, source_entity, target_entity, report_fn)

        if status == "paused":
            progress = await self.db.get_progress(job_id)
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message=f"搬移暫停\n"
                        f"進度：{progress['success']}/{progress['total']}\n"
                        f"請選擇：重試 / 跳過 / 一律跳過",
            )

        del self._pending_jobs[task_id]
        progress = await self.db.get_progress(job_id)
        return AgentResult(
            status=TaskStatus.DONE,
            message=f"搬移完成\n"
                    f"來源：{job['source_chat']}\n"
                    f"目標：{job['target_chat']}\n"
                    f"成功：{progress['success']} 則\n"
                    f"跳過：{progress['skipped']} 則\n"
                    f"失敗：{progress['failed']} 則",
        )

    async def _ai_parse_batch(self, content: str) -> dict | None:
        """Use Gemini Flash to parse natural language batch command."""
        prompt = (
            "你是一個指令解析器。從以下使用者訊息中提取搬移參數，回覆 JSON：\n"
            '{"source": "@channel 或連結", "target": "@channel 或連結 或 null", '
            '"filter_type": "all 或 count 或 date_range", '
            '"filter_value_raw": null 或 {"count": N} 或 {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}}\n\n'
            f"使用者訊息：{content}\n\n只回覆 JSON，不要解釋。"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "gemini", "-p", prompt, "-m", "gemini-2.5-flash",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            import json
            text = stdout.decode().strip()
            # Extract JSON from response (may be wrapped in markdown)
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            return json.loads(text)
        except Exception as e:
            logger.error(f"AI parse failed: {e}")
            return None

    async def _count_messages(self, entity, filter_type: str, filter_value) -> int:
        """Estimate message count based on filter."""
        if filter_type == "count" and filter_value:
            return filter_value.get("count", 0) if isinstance(filter_value, dict) else int(filter_value)

        # For date_range and all, iterate and count
        count = 0
        async for msg in self.tg_client.iter_messages(entity, limit=None):
            if filter_type == "date_range" and filter_value:
                from datetime import datetime
                msg_date = msg.date.strftime("%Y-%m-%d")
                if isinstance(filter_value, dict):
                    if msg_date < filter_value.get("from", ""):
                        break
                    if msg_date > filter_value.get("to", ""):
                        continue
            count += 1
            if count >= 10000:  # Safety limit for estimation
                break
        return count

    async def _collect_messages(self, entity, filter_type: str, filter_value) -> list:
        """Collect all messages matching the filter."""
        messages = []
        limit = None

        if filter_type == "count" and filter_value:
            limit = filter_value.get("count", 100) if isinstance(filter_value, dict) else int(filter_value)

        async for msg in self.tg_client.iter_messages(entity, limit=limit):
            if filter_type == "date_range" and filter_value:
                msg_date = msg.date.strftime("%Y-%m-%d")
                if isinstance(filter_value, dict):
                    if msg_date < filter_value.get("from", ""):
                        break
                    if msg_date > filter_value.get("to", ""):
                        continue
            messages.append(msg)

        messages.reverse()  # Oldest first
        return messages

    async def run(self) -> None:
        await self._init_services()
        await super().run()


async def main():
    logging.basicConfig(level=logging.INFO)
    hub_url = os.environ.get("HUB_URL", "http://localhost:9000")
    port = int(os.environ.get("AGENT_PORT", "0"))

    agent = TGTransferAgent(hub_url=hub_url, port=port)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_tg_transfer_agent.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/__main__.py tests/test_tg_transfer_agent.py
git commit -m "feat(tg-transfer): add main agent with handle_task dispatch logic"
```

---

### Task 8: Dockerfile & Docker Compose

**Files:**
- Create: `agents/tg_transfer/Dockerfile`
- Create: `.env/tg-transfer-agent.env`
- Modify: `docker-compose.yaml`

- [ ] **Step 1: Create Dockerfile**

```dockerfile
FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir aiosqlite>=0.20

COPY core/ ./core/
COPY agents/ ./agents/

CMD ["python", "-m", "agents.tg_transfer"]
```

- [ ] **Step 2: Create .env file**

```env
HUB_URL=http://hub:9000
AGENT_HOST=tg-transfer-agent
AGENT_PORT=8011
TG_API_ID=YOUR_API_ID
TG_API_HASH=YOUR_API_HASH
DATA_DIR=/data/tg_transfer
```

- [ ] **Step 3: Add service to docker-compose.yaml**

Add the following service block after the `claude-code-agent` service:

```yaml
  tg-transfer-agent:
    build:
      context: .
      dockerfile: agents/tg_transfer/Dockerfile
    env_file:
      - .env/tg-transfer-agent.env
    volumes:
      - ./data/tg_transfer:/data/tg_transfer
    depends_on:
      hub:
        condition: service_healthy
    networks:
      - agent-network
```

- [ ] **Step 4: Commit**

```bash
git add agents/tg_transfer/Dockerfile .env/tg-transfer-agent.env docker-compose.yaml
git commit -m "feat(tg-transfer): add Dockerfile and docker-compose service"
```

---

### Task 9: Integration Test — End-to-End Smoke Test

**Files:**
- Create: `tests/test_tg_transfer_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_tg_transfer_integration.py
"""Integration tests for TG Transfer Agent — tests the full flow without real Telegram."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import web
from core.models import TaskRequest, TaskStatus


@pytest.fixture
async def db(tmp_path):
    from agents.tg_transfer.db import TransferDB
    database = TransferDB(str(tmp_path / "integration.db"))
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def mock_tg_client():
    client = AsyncMock()

    def make_msg(msg_id, text="test", media=None, grouped_id=None):
        m = MagicMock()
        m.id = msg_id
        m.text = text
        m.message = text
        m.media = media
        m.grouped_id = grouped_id
        m.photo = None
        m.video = None
        m.document = None
        m.sticker = None
        m.poll = None
        m.voice = None
        m.date = MagicMock()
        m.date.strftime = MagicMock(return_value="2026-04-17")
        return m

    client._make_msg = make_msg
    return client


@pytest.mark.asyncio
async def test_single_transfer_via_link(db, mock_tg_client):
    """User pastes a TG link → agent downloads and re-uploads."""
    from agents.tg_transfer.transfer_engine import TransferEngine

    msg = mock_tg_client._make_msg(123, text="photo caption")
    mock_tg_client.get_messages = AsyncMock(return_value=msg)
    mock_tg_client.get_entity = AsyncMock(return_value=MagicMock())
    mock_tg_client.send_message = AsyncMock()

    engine = TransferEngine(client=mock_tg_client, db=db, tmp_dir="/tmp/test_transfer")
    target = MagicMock()
    source = MagicMock()

    result = await engine.transfer_single(source, target, msg)
    assert result is True
    mock_tg_client.send_message.assert_called_once_with(target, "photo caption")


@pytest.mark.asyncio
async def test_batch_with_dedup(db, mock_tg_client):
    """Second batch run on same source→target should skip already-done messages."""
    # First job: messages 1, 2, 3 all success
    job1 = await db.create_job("@src", "@dst", "batch")
    await db.add_messages(job1, [1, 2, 3])
    await db.mark_message(job1, 1, "success")
    await db.mark_message(job1, 2, "success")
    await db.mark_message(job1, 3, "success")
    await db.update_job_status(job1, "completed")

    # Check dedup
    already = await db.get_transferred_message_ids("@src", "@dst")
    assert already == {1, 2, 3}

    # New job should skip these
    job2 = await db.create_job("@src", "@dst", "batch")
    new_msg_ids = [1, 2, 3, 4, 5]
    to_add = [mid for mid in new_msg_ids if mid not in already]
    assert to_add == [4, 5]


@pytest.mark.asyncio
async def test_config_persistence(db):
    """Config set via bot should persist and be retrievable."""
    await db.set_config("default_target_chat", "@first")
    assert await db.get_config("default_target_chat") == "@first"

    await db.set_config("default_target_chat", "@second")
    assert await db.get_config("default_target_chat") == "@second"
```

- [ ] **Step 2: Run integration tests**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_tg_transfer_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/ -v`
Expected: All PASS (existing tests should not break)

- [ ] **Step 4: Commit**

```bash
git add tests/test_tg_transfer_integration.py
git commit -m "test(tg-transfer): add integration tests for full transfer flow"
```

---

### Task 10: Add aiosqlite Dependency

> **Note:** Execute this task BEFORE Task 3 (Database Layer), since DB tests require aiosqlite.

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add aiosqlite to requirements.txt**

Add this line to `requirements.txt`:

```
aiosqlite>=0.20,<1
```

- [ ] **Step 2: Install and verify**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && pip install aiosqlite>=0.20`
Expected: Successfully installed

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: add aiosqlite dependency for tg-transfer agent"
```
