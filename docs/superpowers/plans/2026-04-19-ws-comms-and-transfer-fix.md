# WebSocket 通訊層 + tg-transfer 傳輸修復 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 inter-service 通訊從 HTTP request-response 改為 WebSocket 長連線，並修復 tg-transfer 的影片上傳 metadata 缺失和 album 處理問題。

**Architecture:** Hub 作為 WebSocket server，Gateway 和 Agent 作為 client 主動連入。所有 task 派發和結果回報走 WS。Agent 註冊保留 HTTP 一次性呼叫。Dashboard 相關 endpoint 不變。

**Tech Stack:** Python 3.12, aiohttp (WebSocket server/client), Telethon, ffprobe, asyncio

---

## File Structure

| 檔案 | 角色 | 改動類型 |
|------|------|----------|
| `agents/tg_transfer/media_utils.py` | ffprobe metadata 提取 | **新建** |
| `agents/tg_transfer/transfer_engine.py` | 影片 metadata、並行下載、album 原子性、cancel | 修改 |
| `core/ws.py` | WS 訊息協議常數 + 共用 helper | **新建** |
| `hub/ws_handler.py` | Hub WS endpoint (agent + gateway) | **新建** |
| `hub/server.py` | 掛載 WS route、移除 /heartbeat、dispatch 改走 WS | 修改 |
| `hub/registry.py` | WS 連線狀態取代 heartbeat | 修改 |
| `hub/dashboard.py` | 顯示 Gateway 連線資訊、task 終止按鈕改走 WS | 修改 |
| `hub/cli.py` | 移除 `send_task_to_agent`、保留 CLI 互動 | 修改 |
| `core/base_agent.py` | WS client、移除 heartbeat、WS message handler | 修改 |
| `core/models.py` | TaskRequest 加 chat_id | 修改 |
| `gateway/telegram_user_handler.py` | WS client 取代 HTTP dispatch | 修改 |
| `gateway/telegram_handler.py` | WS client 取代 HTTP dispatch | 修改 |
| `gateway/__main__.py` | 改用 async 啟動以支援 WS | 修改 |
| `tests/test_media_utils.py` | ffprobe helper 測試 | **新建** |
| `tests/test_transfer_engine.py` | album 原子性、cancel 測試 | 修改 |
| `tests/test_ws.py` | WS 訊息協議測試 | **新建** |
| `tests/test_hub_ws.py` | Hub WS handler 測試 | **新建** |
| `tests/test_base_agent_ws.py` | Agent WS client 測試 | **新建** |

---

## Task 1: ffprobe metadata helper

**Files:**
- Create: `agents/tg_transfer/media_utils.py`
- Create: `tests/test_media_utils.py`

- [ ] **Step 1: Write test for ffprobe_metadata**

```python
# tests/test_media_utils.py
import pytest
import asyncio
import os
import subprocess

from agents.tg_transfer.media_utils import ffprobe_metadata


def _has_ffprobe():
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True)
        return True
    except FileNotFoundError:
        return False


@pytest.fixture
def sample_video(tmp_path):
    """Create a minimal MP4 with ffmpeg for testing."""
    path = str(tmp_path / "test.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        "color=c=black:s=320x240:d=2", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", path,
    ], capture_output=True)
    return path


@pytest.mark.skipif(not _has_ffprobe(), reason="ffprobe not installed")
@pytest.mark.asyncio
async def test_ffprobe_metadata(sample_video):
    meta = await ffprobe_metadata(sample_video)
    assert meta is not None
    assert meta["width"] == 320
    assert meta["height"] == 240
    assert meta["duration"] >= 1


@pytest.mark.asyncio
async def test_ffprobe_metadata_nonexistent():
    meta = await ffprobe_metadata("/tmp/does_not_exist.mp4")
    assert meta is None


@pytest.mark.skipif(not _has_ffprobe(), reason="ffprobe not installed")
@pytest.mark.asyncio
async def test_ffprobe_metadata_non_video(tmp_path):
    path = str(tmp_path / "text.txt")
    with open(path, "w") as f:
        f.write("not a video")
    meta = await ffprobe_metadata(path)
    assert meta is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_media_utils.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.tg_transfer.media_utils'`

- [ ] **Step 3: Implement ffprobe_metadata**

```python
# agents/tg_transfer/media_utils.py
import asyncio
import json
import logging

logger = logging.getLogger(__name__)


async def ffprobe_metadata(file_path: str) -> dict | None:
    """Extract video metadata using ffprobe.

    Returns {"duration": int, "width": int, "height": int} or None on failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-select_streams", "v:0", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return None

        data = json.loads(stdout)
        streams = data.get("streams", [])
        if not streams:
            return None

        stream = streams[0]
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))

        # Duration can be in stream or format level
        duration_str = stream.get("duration")
        if not duration_str:
            # Try format level
            proc2 = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)
            if proc2.returncode == 0:
                fmt = json.loads(stdout2).get("format", {})
                duration_str = fmt.get("duration")

        duration = int(float(duration_str)) if duration_str else 0

        if width == 0 or height == 0:
            return None

        return {"duration": duration, "width": width, "height": height}
    except Exception as e:
        logger.debug(f"ffprobe failed for {file_path}: {e}")
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_media_utils.py -v`
Expected: All tests PASS (skipif tests may skip if ffprobe not installed locally)

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/media_utils.py tests/test_media_utils.py
git commit -m "feat(tg-transfer): add ffprobe metadata helper"
```

---

## Task 2: 影片上傳帶 metadata + supports_streaming

**Files:**
- Modify: `agents/tg_transfer/transfer_engine.py:85-159` (`_transfer_media`)
- Modify: `agents/tg_transfer/transfer_engine.py:60-83` (`transfer_album`)

- [ ] **Step 1: Write test for video upload with metadata**

在 `tests/test_transfer_engine.py` 新增：

```python
# tests/test_transfer_engine.py — 新增以下

from unittest.mock import patch, AsyncMock, MagicMock
import os


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

    # Mock download to create a file
    video_path = str(tmp_path / "downloads" / "200" / "video.mp4")
    os.makedirs(os.path.dirname(video_path), exist_ok=True)
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 100)
    mock_client.download_media = AsyncMock(return_value=video_path)

    # Mock send_file
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_transfer_engine.py::test_transfer_media_video_sends_attributes -v`
Expected: FAIL — `send_file` 被呼叫時沒有 `attributes` 或 `supports_streaming`

- [ ] **Step 3: Modify `_transfer_media` to add video metadata**

修改 `agents/tg_transfer/transfer_engine.py`：

在檔案頂部加入 import：
```python
from agents.tg_transfer.media_utils import ffprobe_metadata
from telethon.tl.types import DocumentAttributeVideo
```

將 `_transfer_media` 的上傳部分（原 line 136-138）改為：

```python
            # Build upload kwargs
            upload_kwargs = {"caption": message.text}

            # Add video metadata if applicable
            if file_type == "video":
                meta = await ffprobe_metadata(path)
                if meta:
                    upload_kwargs["attributes"] = [DocumentAttributeVideo(
                        duration=meta["duration"],
                        w=meta["width"],
                        h=meta["height"],
                        supports_streaming=True,
                    )]
                    upload_kwargs["supports_streaming"] = True

            # Upload
            result = await self.client.send_file(
                target_entity, path, **upload_kwargs
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_transfer_engine.py -v`
Expected: All tests PASS

- [ ] **Step 5: Modify `transfer_album` to add video metadata per file**

修改 `transfer_album` 方法，從：

```python
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
```

改為：

```python
        files = []
        caption = None
        try:
            for msg in messages:
                path = await self.client.download_media(msg, file=job_dir)
                if path:
                    files.append((path, msg))
                if msg.text and not caption:
                    caption = msg.text

            if not files:
                return False

            file_paths = [f[0] for f in files]

            # Build per-file attributes for videos
            file_attrs = []
            for path, msg in files:
                if msg.video:
                    meta = await ffprobe_metadata(path)
                    if meta:
                        file_attrs.append(DocumentAttributeVideo(
                            duration=meta["duration"],
                            w=meta["width"],
                            h=meta["height"],
                            supports_streaming=True,
                        ))
                    else:
                        file_attrs.append(None)
                else:
                    file_attrs.append(None)

            await self.client.send_file(
                target_entity, file_paths, caption=caption,
            )
            return True
```

- [ ] **Step 6: Run all tests**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_transfer_engine.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add agents/tg_transfer/transfer_engine.py
git commit -m "feat(tg-transfer): video upload with metadata + supports_streaming"
```

---

## Task 3: Album 並行下載 + 原子性

**Files:**
- Modify: `agents/tg_transfer/transfer_engine.py:60-83` (`transfer_album`)
- Modify: `tests/test_transfer_engine.py`

- [ ] **Step 1: Write test for album atomicity (download failure)**

在 `tests/test_transfer_engine.py` 新增：

```python
@pytest.mark.asyncio
async def test_transfer_album_atomic_download_failure(engine, mock_client, tmp_path):
    """If any media in album fails to download, entire album should fail."""
    target_entity = MagicMock()
    msg1 = _make_message(301, text="caption", grouped_id=10)
    msg2 = _make_message(302, grouped_id=10)

    # First download succeeds, second returns None
    download_results = [str(tmp_path / "file1.jpg"), None]
    mock_client.download_media = AsyncMock(side_effect=download_results)
    mock_client.send_file = AsyncMock()

    with patch("agents.tg_transfer.transfer_engine.ffprobe_metadata", new_callable=AsyncMock, return_value=None):
        result = await engine.transfer_album(target_entity, [msg1, msg2])

    assert result is False
    mock_client.send_file.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_album_parallel_download(engine, mock_client, tmp_path):
    """Album downloads should run concurrently via asyncio.gather."""
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

    with patch("agents.tg_transfer.transfer_engine.ffprobe_metadata", new_callable=AsyncMock, return_value=None):
        result = await engine.transfer_album(target_entity, [msg1, msg2])

    assert result is True
    mock_client.send_file.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_transfer_engine.py::test_transfer_album_atomic_download_failure tests/test_transfer_engine.py::test_transfer_album_parallel_download -v`
Expected: `test_transfer_album_atomic_download_failure` FAILS (current code sends partial albums)

- [ ] **Step 3: Rewrite `transfer_album` with parallel download + atomicity**

Replace the entire `transfer_album` method in `agents/tg_transfer/transfer_engine.py`:

```python
    async def transfer_album(self, target_entity, messages: list) -> bool:
        """Transfer a media group (album) as a single album.
        Atomic: if any download fails, nothing is uploaded.
        """
        job_dir = os.path.join(self.tmp_dir, "album")
        os.makedirs(job_dir, exist_ok=True)

        caption = None
        for msg in messages:
            if msg.text and not caption:
                caption = msg.text

        try:
            # Parallel download
            download_tasks = [
                self.client.download_media(msg, file=job_dir)
                for msg in messages
            ]
            paths = await asyncio.gather(*download_tasks)

            # Atomic check: all must succeed
            if any(p is None for p in paths):
                return False

            file_paths = list(paths)

            await self.client.send_file(
                target_entity, file_paths, caption=caption,
            )
            return True
        finally:
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_transfer_engine.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/transfer_engine.py tests/test_transfer_engine.py
git commit -m "feat(tg-transfer): album parallel download + atomic upload"
```

---

## Task 4: WS 訊息協議 + 共用常數

**Files:**
- Create: `core/ws.py`
- Create: `tests/test_ws.py`

- [ ] **Step 1: Write test for WS message helpers**

```python
# tests/test_ws.py
import json
from core.ws import ws_msg, MsgType


def test_ws_msg_dispatch():
    msg = ws_msg(MsgType.DISPATCH, chat_id=123, message="hello")
    parsed = json.loads(msg)
    assert parsed["type"] == "dispatch"
    assert parsed["chat_id"] == 123
    assert parsed["message"] == "hello"


def test_ws_msg_result():
    msg = ws_msg(MsgType.RESULT, task_id="abc", status="done", message="ok")
    parsed = json.loads(msg)
    assert parsed["type"] == "result"
    assert parsed["task_id"] == "abc"
    assert parsed["status"] == "done"


def test_ws_msg_progress():
    msg = ws_msg(MsgType.PROGRESS, task_id="abc", chat_id=123, message="50%")
    parsed = json.loads(msg)
    assert parsed["type"] == "progress"
    assert parsed["task_id"] == "abc"
    assert parsed["chat_id"] == 123


def test_ws_msg_cancel():
    msg = ws_msg(MsgType.CANCEL, task_id="abc")
    parsed = json.loads(msg)
    assert parsed["type"] == "cancel"
    assert parsed["task_id"] == "abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_ws.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement ws.py**

```python
# core/ws.py
"""WebSocket message protocol constants and helpers."""
import json
from enum import Enum


class MsgType(str, Enum):
    # Gateway → Hub
    DISPATCH = "dispatch"

    # Hub → Gateway
    REPLY = "reply"

    # Hub → Agent
    TASK = "task"
    CANCEL = "cancel"

    # Agent → Hub
    RESULT = "result"

    # Bidirectional (Agent → Hub → Gateway)
    PROGRESS = "progress"

    # Gateway → Hub (on connect)
    GW_REGISTER = "gw_register"


def ws_msg(msg_type: MsgType, **kwargs) -> str:
    """Build a JSON WS message string."""
    payload = {"type": msg_type.value, **kwargs}
    return json.dumps(payload, ensure_ascii=False)


def ws_parse(raw: str) -> dict:
    """Parse a WS message string into a dict."""
    return json.loads(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_ws.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/ws.py tests/test_ws.py
git commit -m "feat(core): WS message protocol constants and helpers"
```

---

## Task 5: Hub WS handler (agent + gateway endpoint)

**Files:**
- Create: `hub/ws_handler.py`
- Modify: `hub/server.py:42-75` (掛載 WS route)
- Modify: `hub/registry.py` (WS 連線狀態)
- Create: `tests/test_hub_ws.py`

- [ ] **Step 1: Write test for Hub WS agent connection**

```python
# tests/test_hub_ws.py
import pytest
import json
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from hub.server import create_hub_app
from core.ws import MsgType


@pytest.fixture
def hub_app(tmp_path):
    return create_hub_app(
        heartbeat_timeout=30,
        use_gemini_fallback=False,
        db_path=str(tmp_path / "test.db"),
    )


@pytest.mark.asyncio
async def test_agent_ws_connect_and_receive_task(aiohttp_client, hub_app):
    """Agent connects via WS, Hub can send task through WS."""
    client = await aiohttp_client(hub_app)

    # Register agent first via HTTP
    await client.post("/register", json={
        "name": "test-agent",
        "description": "test",
        "url": "http://unused",
        "route_patterns": ["test"],
        "capabilities": [],
        "priority": 0,
    })

    # Connect WS
    ws = await client.ws_connect("/ws/agent/test-agent")

    # Agent should now be tracked as WS-connected
    registry = hub_app["registry"]
    assert registry.has_ws("test-agent") is True

    await ws.close()


@pytest.mark.asyncio
async def test_gateway_ws_connect(aiohttp_client, hub_app):
    """Gateway connects via WS, Hub tracks it."""
    client = await aiohttp_client(hub_app)
    ws = await client.ws_connect("/ws/gateway")

    # Send gw_register
    await ws.send_json({
        "type": "gw_register",
        "mode": "userbot",
        "phone": "+886***908",
        "allowed_chats": [-100123],
    })

    # Hub should track this gateway
    assert len(hub_app["gateway_connections"]) == 1

    await ws.close()


@pytest.mark.asyncio
async def test_agent_ws_disconnect_marks_offline(aiohttp_client, hub_app):
    """When agent WS disconnects, registry marks it offline."""
    client = await aiohttp_client(hub_app)

    await client.post("/register", json={
        "name": "dc-agent",
        "description": "test",
        "url": "http://unused",
        "route_patterns": [],
        "capabilities": [],
        "priority": 0,
    })

    ws = await client.ws_connect("/ws/agent/dc-agent")
    assert hub_app["registry"].has_ws("dc-agent") is True

    await ws.close()
    # After close, the handler loop should clean up
    # Give event loop a tick
    import asyncio
    await asyncio.sleep(0.1)

    assert hub_app["registry"].has_ws("dc-agent") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_hub_ws.py -v`
Expected: FAIL — no WS routes exist

- [ ] **Step 3: Add `has_ws` and `set_ws`/`remove_ws` to Registry**

修改 `hub/registry.py`：

在 `__init__` 加入：
```python
        self._ws_connections: dict[str, web.WebSocketResponse] = {}  # name → ws
```

加入新方法：
```python
    def set_ws(self, name: str, ws: "web.WebSocketResponse"):
        self._ws_connections[name] = ws
        self._last_heartbeat[name] = time.time()

    def remove_ws(self, name: str):
        self._ws_connections.pop(name, None)

    def has_ws(self, name: str) -> bool:
        ws = self._ws_connections.get(name)
        return ws is not None and not ws.closed

    def get_ws(self, name: str) -> "web.WebSocketResponse | None":
        ws = self._ws_connections.get(name)
        if ws and not ws.closed:
            return ws
        return None
```

修改 `_is_alive` 方法，優先用 WS 狀態：
```python
    def _is_alive(self, name: str) -> bool:
        # WS connection takes priority
        if self.has_ws(name):
            return True
        # Fallback to heartbeat (for agents not yet on WS)
        last = self._last_heartbeat.get(name, 0)
        return (time.time() - last) < self._heartbeat_timeout
```

- [ ] **Step 4: Implement `hub/ws_handler.py`**

```python
# hub/ws_handler.py
"""WebSocket handlers for Hub — agent and gateway connections."""
import json
import logging
from aiohttp import web, WSMsgType

from core.ws import MsgType, ws_msg, ws_parse
from core.models import TaskRequest
from hub.task_manager import TaskManager

logger = logging.getLogger(__name__)


async def handle_agent_ws(request: web.Request) -> web.WebSocketResponse:
    """WS endpoint for agent connections: /ws/agent/{name}"""
    name = request.match_info["name"]
    registry = request.app["registry"]
    task_manager: TaskManager = request.app["task_manager"]

    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    registry.set_ws(name, ws)
    logger.info(f"Agent WS connected: {name}")

    try:
        async for raw_msg in ws:
            if raw_msg.type == WSMsgType.TEXT:
                data = ws_parse(raw_msg.data)
                msg_type = data.get("type")

                if msg_type == MsgType.RESULT.value:
                    await _handle_agent_result(request.app, data)

                elif msg_type == MsgType.PROGRESS.value:
                    await _forward_progress_to_gateway(request.app, data)

            elif raw_msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        registry.remove_ws(name)
        logger.info(f"Agent WS disconnected: {name}")

        # Mark all active tasks for this agent as error
        _close_agent_tasks(task_manager, name, request.app)

    return ws


async def _handle_agent_result(app: web.Application, data: dict):
    """Process result message from agent, forward to gateway."""
    task_manager: TaskManager = app["task_manager"]
    task_id = data.get("task_id")
    status = data.get("status")
    message = data.get("message", "")

    task = task_manager.get_task(task_id)
    if not task:
        return

    # Update task status
    if status == "done":
        task_manager.complete_task(task_id)
    elif status in ("need_input", "need_approval"):
        task_manager.update_status(task_id, f"waiting_{status.split('_', 1)[-1] if '_' in status else status}")
    elif status == "error":
        task_manager.update_status(task_id, "done")
    elif status == "cancelled":
        task_manager.close_task(task_id)

    if message:
        task_manager.append_assistant_response(task_id, message)

    # Forward to gateway
    chat_id = task["chat_id"]
    reply = ws_msg(MsgType.REPLY,
        chat_id=chat_id,
        task_id=task_id,
        message=message,
        status=status,
        options=data.get("options"),
    )
    await _send_to_gateway(app, chat_id, reply)


async def _forward_progress_to_gateway(app: web.Application, data: dict):
    """Forward progress message from agent to gateway."""
    chat_id = data.get("chat_id")
    if chat_id is None:
        return
    progress = ws_msg(MsgType.PROGRESS,
        chat_id=chat_id,
        task_id=data.get("task_id"),
        message=data.get("message", ""),
    )
    await _send_to_gateway(app, chat_id, progress)


async def _send_to_gateway(app: web.Application, chat_id: int, message: str):
    """Send a message to the appropriate gateway connection."""
    gateways: list[dict] = app.get("gateway_connections", [])
    for gw in gateways:
        ws = gw.get("ws")
        if ws and not ws.closed:
            await ws.send_str(message)
            return


def _close_agent_tasks(task_manager: TaskManager, agent_name: str, app: web.Application):
    """Mark all active tasks for a disconnected agent as error."""
    import time as _time
    rows = task_manager._conn.execute(
        "SELECT * FROM tasks WHERE agent_name = ? AND status IN ('working', 'waiting_input', 'waiting_approval')",
        (agent_name,),
    ).fetchall()
    for row in rows:
        task = task_manager._row_to_dict(row)
        task_manager.update_status(task["task_id"], "done")
        task_manager.append_assistant_response(task["task_id"], "Agent 已離線，任務已中斷")

        # Notify gateway
        import asyncio
        msg = ws_msg(MsgType.REPLY,
            chat_id=task["chat_id"],
            task_id=task["task_id"],
            message="Agent 已離線，任務已中斷",
            status="error",
        )
        asyncio.ensure_future(_send_to_gateway(app, task["chat_id"], msg))


async def handle_gateway_ws(request: web.Request) -> web.WebSocketResponse:
    """WS endpoint for gateway connections: /ws/gateway"""
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)

    gw_info = {"ws": ws, "mode": None, "phone": None, "allowed_chats": None}
    gateways: list[dict] = request.app["gateway_connections"]
    gateways.append(gw_info)
    logger.info("Gateway WS connected")

    try:
        async for raw_msg in ws:
            if raw_msg.type == WSMsgType.TEXT:
                data = ws_parse(raw_msg.data)
                msg_type = data.get("type")

                if msg_type == MsgType.GW_REGISTER.value:
                    gw_info["mode"] = data.get("mode")
                    gw_info["phone"] = data.get("phone")
                    gw_info["allowed_chats"] = data.get("allowed_chats")

                elif msg_type == MsgType.DISPATCH.value:
                    await _handle_gateway_dispatch(request.app, ws, data)

            elif raw_msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        gateways.remove(gw_info)
        logger.info("Gateway WS disconnected")

    return ws


async def _handle_gateway_dispatch(app: web.Application, gw_ws: web.WebSocketResponse, data: dict):
    """Handle dispatch message from gateway — same logic as HTTP /dispatch but over WS."""
    from hub.router import Router
    from hub.gemini_fallback import gemini_unified_route, GeminiChat

    message = data.get("message", "")
    chat_id = data.get("chat_id", 0)
    reply_to_message_id = data.get("reply_to_message_id")
    source = data.get("source", "telegram")

    task_manager: TaskManager = app["task_manager"]
    registry = app["registry"]
    chat: GeminiChat = app["chat"]

    task_manager.run_lifecycle()

    # Handle /clear
    if message.strip() == "/clear":
        active = task_manager.get_active_task_for_chat(chat_id)
        if active:
            task_manager.complete_task(active["task_id"])
            await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=None, message="對話已結束", status="done"))
        else:
            await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=None, message="沒有進行中的對話", status="done"))
        return

    # Priority 1: Reply to specific message → exact task
    if reply_to_message_id:
        task = task_manager.get_task_by_message_id(chat_id, reply_to_message_id)
        if task and task["status"] not in ("closed",):
            if task["status"] in ("done", "archived"):
                task_manager.update_status(task["task_id"], "working")
            await _continue_task_ws(app, gw_ws, task, message, chat_id)
            return

    # Priority 2: Active task waiting for input
    active_task = task_manager.get_active_task_for_chat(chat_id)
    if active_task and active_task["status"] in ("waiting_input", "waiting_approval"):
        await _continue_task_ws(app, gw_ws, active_task, message, chat_id)
        return

    # Priority 3: Keyword match
    router: Router = app["router"]
    keyword_match = router.match_by_keyword(message)
    if keyword_match:
        task = task_manager.create_task(agent_name=keyword_match.name, chat_id=chat_id, content=message, source=source)
        await _dispatch_to_agent_ws(app, gw_ws, task, message, chat_id)
        return

    # Priority 4: Gemini routing
    if app.get("use_gemini_fallback"):
        import os, time as _time
        expiry_days = int(os.environ.get("TASK_EXPIRY_DAYS", "7"))
        expiry = _time.time() - (expiry_days * 86400)
        rows = task_manager._conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status NOT IN ('archived', 'closed') AND updated_at > ? ORDER BY updated_at DESC LIMIT 10",
            (chat_id, expiry),
        ).fetchall()
        active_tasks = [task_manager._row_to_dict(r) for r in rows]
        online_agents = [a for a in registry.list_online() if a.priority >= 0]

        decision = await gemini_unified_route(message, active_tasks, online_agents)
        action = decision.get("action")

        if action == "continue":
            task = task_manager.get_task(decision["task_id"])
            if task:
                await _continue_task_ws(app, gw_ws, task, message, chat_id)
                return

        elif action == "route":
            agent_name = decision["agent_name"]
            agent_info = registry.get(agent_name)
            if agent_info:
                task = task_manager.create_task(agent_name=agent_name, chat_id=chat_id, content=message, source=source)
                await _dispatch_to_agent_ws(app, gw_ws, task, message, chat_id)
                return

        # Fallback: hub chat
        await _hub_chat_reply_ws(app, gw_ws, chat_id, message, source)
        return

    await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=None, message="無法處理此訊息", status="error"))


async def _continue_task_ws(app: web.Application, gw_ws, task: dict, message: str, chat_id: int):
    """Continue existing task over WS."""
    task_manager: TaskManager = app["task_manager"]
    chat: "GeminiChat" = app["chat"]
    task_manager.append_user_response(task["task_id"], message)
    task = task_manager.get_task(task["task_id"])

    if task["agent_name"] == "_hub":
        reply = await chat.reply_with_context(task["conversation_history"])
        if reply:
            task_manager.append_assistant_response(task["task_id"], reply)
            task_manager.complete_task(task["task_id"])
            await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=task["task_id"], message=reply, status="done"))
        else:
            await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=task["task_id"], message="無法處理此訊息", status="error"))
        return

    # Forward to agent via WS
    registry = app["registry"]
    agent_ws = registry.get_ws(task["agent_name"])
    if not agent_ws:
        await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=task["task_id"], message="Agent 已離線", status="error"))
        return

    await agent_ws.send_str(ws_msg(MsgType.TASK,
        task_id=task["task_id"],
        content=message,
        conversation_history=task["conversation_history"],
        chat_id=chat_id,
    ))


async def _dispatch_to_agent_ws(app: web.Application, gw_ws, task: dict, message: str, chat_id: int):
    """Dispatch new task to agent via WS."""
    registry = app["registry"]
    agent_ws = registry.get_ws(task["agent_name"])
    if not agent_ws:
        await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=task["task_id"], message="Agent 已離線", status="error"))
        return

    await agent_ws.send_str(ws_msg(MsgType.TASK,
        task_id=task["task_id"],
        content=message,
        conversation_history=task["conversation_history"],
        chat_id=chat_id,
    ))


async def _hub_chat_reply_ws(app: web.Application, gw_ws, chat_id: int, message: str, source: str):
    """Hub direct chat reply via Gemini."""
    from hub.gemini_fallback import GeminiChat
    task_manager: TaskManager = app["task_manager"]
    chat: GeminiChat = app["chat"]

    active = task_manager.get_active_task_for_chat(chat_id)
    if active and active["agent_name"] == "_hub":
        task_manager.append_user_response(active["task_id"], message)
        task = task_manager.get_task(active["task_id"])
        reply = await chat.reply_with_context(task["conversation_history"])
    else:
        reply = await chat.reply(message)
        if reply:
            task = task_manager.create_task(agent_name="_hub", chat_id=chat_id, content=message, source=source)
        else:
            await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=None, message="無法處理此訊息", status="error"))
            return

    if reply:
        task_manager.append_assistant_response(task["task_id"], reply)
        task_manager.complete_task(task["task_id"])
        await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=task["task_id"], message=reply, status="done"))
    else:
        await gw_ws.send_str(ws_msg(MsgType.REPLY, chat_id=chat_id, task_id=None, message="無法處理此訊息", status="error"))
```

- [ ] **Step 5: Mount WS routes in `hub/server.py`**

在 `hub/server.py` 加入 import：
```python
from hub.ws_handler import handle_agent_ws, handle_gateway_ws
```

在 `create_hub_app` 裡 `app["chat"] = chat` 之後加入：
```python
    app["gateway_connections"] = []  # list of {"ws": ..., "mode": ..., "phone": ..., "allowed_chats": ...}
```

在路由定義區塊加入 WS routes（在 API routes 區塊）：
```python
    # WebSocket routes
    app.router.add_get("/ws/agent/{name}", handle_agent_ws)
    app.router.add_get("/ws/gateway", handle_gateway_ws)
```

在 `auth_middleware` 的 `no_auth_prefixes` 裡加入 `"/ws/"`:
```python
        no_auth_prefixes = ("/register", "/heartbeat", "/agents", "/dispatch", "/set_message_id", "/auth/", "/ws/")
```

- [ ] **Step 6: Run tests**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_hub_ws.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add hub/ws_handler.py hub/server.py hub/registry.py tests/test_hub_ws.py
git commit -m "feat(hub): WebSocket handler for agent and gateway connections"
```

---

## Task 6: BaseAgent WS client

**Files:**
- Modify: `core/base_agent.py`
- Modify: `core/models.py` (TaskRequest 加 chat_id)

- [ ] **Step 1: Add chat_id to TaskRequest**

修改 `core/models.py`：

```python
@dataclass
class TaskRequest:
    task_id: str
    content: str
    conversation_history: list[dict] = field(default_factory=list)
    chat_id: int = 0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "content": self.content,
            "conversation_history": self.conversation_history,
            "chat_id": self.chat_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRequest":
        return cls(
            task_id=data["task_id"],
            content=data["content"],
            conversation_history=data.get("conversation_history", []),
            chat_id=data.get("chat_id", 0),
        )
```

- [ ] **Step 2: Rewrite BaseAgent with WS client**

修改 `core/base_agent.py`。保留 HTTP `/health` 和 `/dashboard`，移除 `/task` 和 heartbeat，加入 WS 連線：

```python
# core/base_agent.py
import asyncio
import os
import sys
import logging
from abc import ABC, abstractmethod
from aiohttp import web, ClientSession, WSMsgType
from core.config import load_agent_config
from core.models import AgentInfo, AgentResult, TaskRequest, TaskStatus
from core.sandbox import Sandbox
from core.llm import create_llm_client, check_llm_auth, LLMInitError, LLMClient
from core.ws import MsgType, ws_msg, ws_parse

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    def __init__(self, agent_dir: str, hub_url: str, port: int = 0):
        self.config = load_agent_config(agent_dir)
        self.name = self.config["name"]
        self.hub_url = hub_url
        self.port = port
        self.host = os.environ.get("AGENT_HOST", "localhost")
        sandbox_config = self.config.get("sandbox", {"allowed_dirs": []})
        self.sandbox = Sandbox(sandbox_config)
        self.llm: LLMClient | None = None
        self._llm_authenticated: bool = True
        self._llm_error: str = ""
        self._init_error: str = ""
        self._ws: web.WebSocketResponse | None = None
        self._cancelled_tasks: set[str] = set()

    @abstractmethod
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        pass

    async def _init_services(self) -> None:
        pass

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        return app

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"name": self.name, "status": "ok"})

    async def ws_send(self, data: dict):
        """Send a message over the WS connection to Hub."""
        if self._ws and not self._ws.closed:
            await self._ws.send_str(ws_msg(MsgType(data["type"]), **{k: v for k, v in data.items() if k != "type"}))

    async def ws_send_result(self, task_id: str, result: AgentResult):
        """Send task result to Hub via WS."""
        if self._ws and not self._ws.closed:
            await self._ws.send_str(ws_msg(MsgType.RESULT,
                task_id=task_id,
                status=result.status.value,
                message=result.message,
                options=result.options,
            ))

    async def ws_send_progress(self, task_id: str, chat_id: int, message: str):
        """Send progress update to Hub via WS."""
        if self._ws and not self._ws.closed:
            await self._ws.send_str(ws_msg(MsgType.PROGRESS,
                task_id=task_id,
                chat_id=chat_id,
                message=message,
            ))

    def is_cancelled(self, task_id: str) -> bool:
        """Check if a task has been cancelled."""
        return task_id in self._cancelled_tasks

    async def register(self, actual_port: int) -> None:
        info = AgentInfo(
            name=self.name,
            description=self.config.get("description", ""),
            url=f"http://{self.host}:{actual_port}",
            route_patterns=self.config.get("route_patterns", []),
            capabilities=self.config.get("capabilities", []),
            priority=self.config.get("priority", 0),
        )
        data = info.to_dict()
        if hasattr(self, '_app') and self._app:
            data["has_dashboard"] = any(
                r.resource.canonical == "/dashboard"
                for r in self._app.router.routes()
                if hasattr(r, 'resource') and r.resource
            )
        if self._init_error:
            data["auth_status"] = "error"
            data["auth_error"] = self._init_error
        elif not self._llm_authenticated:
            data["auth_status"] = "unauthenticated"
            data["auth_error"] = self._llm_error
        async with ClientSession() as session:
            await session.post(f"{self.hub_url}/register", json=data)

    async def _ws_loop(self) -> None:
        """Maintain WS connection to Hub. Auto-reconnect on disconnect."""
        ws_url = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/agent/{self.name}"

        while True:
            try:
                async with ClientSession() as session:
                    async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
                        self._ws = ws
                        logger.info(f"WS connected to Hub: {ws_url}")

                        async for msg in ws:
                            if msg.type == WSMsgType.TEXT:
                                data = ws_parse(msg.data)
                                msg_type = data.get("type")

                                if msg_type == MsgType.TASK.value:
                                    asyncio.create_task(self._handle_ws_task(data))

                                elif msg_type == MsgType.CANCEL.value:
                                    task_id = data.get("task_id")
                                    if task_id:
                                        self._cancelled_tasks.add(task_id)
                                        logger.info(f"Task cancelled: {task_id}")

                            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                                break

            except Exception as e:
                logger.warning(f"WS connection lost: {e}")

            self._ws = None
            logger.info("Reconnecting to Hub in 3 seconds...")
            await asyncio.sleep(3)

    async def _handle_ws_task(self, data: dict):
        """Handle incoming task from Hub via WS."""
        task = TaskRequest(
            task_id=data["task_id"],
            content=data["content"],
            conversation_history=data.get("conversation_history", []),
            chat_id=data.get("chat_id", 0),
        )

        try:
            result = await self.handle_task(task)
        except Exception as e:
            logger.error(f"handle_task error: {e}", exc_info=True)
            result = AgentResult(status=TaskStatus.ERROR, message=f"執行失敗：{e}")

        # Clean up cancel flag
        self._cancelled_tasks.discard(task.task_id)

        await self.ws_send_result(task.task_id, result)

    async def run(self) -> None:
        # Check LLM auth if configured
        settings = self.config.get("settings", {})
        if settings.get("llm"):
            auth_ok, auth_error = await check_llm_auth(settings)
            if auth_ok:
                try:
                    self.llm = await create_llm_client(settings)
                except LLMInitError as e:
                    self._llm_authenticated = False
                    self._llm_error = str(e)
                    print(f"WARNING: LLM init failed: {e}", file=sys.stderr)
            else:
                self._llm_authenticated = False
                self._llm_error = auth_error
                print(f"WARNING: LLM not authenticated: {auth_error}", file=sys.stderr)

        # Initialize agent-specific services
        try:
            await self._init_services()
        except Exception as e:
            self._init_error = str(e)
            print(f"WARNING: Service init failed: {e}", file=sys.stderr)

        app = self.create_app()
        self._app = app
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()

        actual_port = site._server.sockets[0].getsockname()[1]
        print(f"Agent '{self.name}' running on port {actual_port}")

        await self.register(actual_port)

        # Start WS connection (runs forever, auto-reconnects)
        await self._ws_loop()
```

- [ ] **Step 3: Run existing agent tests to check backward compat**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/test_base_agent.py tests/test_models.py -v`
Expected: PASS (or update tests for removed `/task` endpoint)

- [ ] **Step 4: Commit**

```bash
git add core/base_agent.py core/models.py
git commit -m "feat(core): BaseAgent WS client, remove heartbeat and /task HTTP"
```

---

## Task 7: Gateway WS client

**Files:**
- Modify: `gateway/telegram_user_handler.py`
- Modify: `gateway/telegram_handler.py`
- Modify: `gateway/__main__.py`

- [ ] **Step 1: Rewrite `telegram_user_handler.py` with WS**

```python
# gateway/telegram_user_handler.py
import asyncio
import json
import logging
from aiohttp import ClientSession, WSMsgType
from telethon import TelegramClient, events
from core.ws import MsgType, ws_msg, ws_parse

logger = logging.getLogger(__name__)

MESSAGE_BATCH_DELAY = 5


class TelegramUserHandler:
    def __init__(self, api_id: int, api_hash: str, phone: str, hub_url: str,
                 session_path: str = "gateway/bot_session",
                 allowed_chats: list[int] | None = None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.hub_url = hub_url
        self.session_path = session_path
        self.allowed_chats = allowed_chats
        self.client = TelegramClient(self.session_path, self.api_id, self.api_hash)

        self._buffers: dict[int, list[tuple[str, any]]] = {}
        self._buffer_timers: dict[int, asyncio.Task] = {}
        self._ws = None
        self._pending_events: dict[int, any] = {}  # chat_id → last event (for reply)

    async def _ws_send_dispatch(self, message: str, chat_id: int,
                                 reply_to_message_id: int | None = None):
        """Send dispatch message to Hub via WS."""
        if not self._ws or self._ws.closed:
            logger.error("WS not connected, cannot dispatch")
            return
        payload = {
            "type": MsgType.DISPATCH.value,
            "message": message,
            "chat_id": chat_id,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        await self._ws.send_json(payload)

    async def _handle_hub_message(self, data: dict):
        """Handle incoming WS message from Hub (reply or progress)."""
        msg_type = data.get("type")
        chat_id = data.get("chat_id")
        text = data.get("message", "")
        status = data.get("status")
        task_id = data.get("task_id")
        options = data.get("options")

        if not chat_id or not text:
            return

        if status == "need_approval":
            text = f"⚠️ {text}"

        if options:
            option_text = "\n".join(f"  {i}. {opt}" for i, opt in enumerate(options, 1))
            text = f"{text}\n\n{option_text}"

        try:
            entity = await self.client.get_entity(chat_id)
            sent = await self.client.send_message(entity, text)

            # Track message_id for reply-based continuation
            if task_id and sent:
                await self._notify_message_id_ws(task_id, sent.id)
        except Exception as e:
            logger.error(f"Failed to send TG message to {chat_id}: {e}")

    async def _notify_message_id_ws(self, task_id: str, message_id: int):
        """Notify Hub of the bot reply message_id via HTTP (kept as HTTP for simplicity)."""
        try:
            async with ClientSession() as session:
                await session.post(
                    f"{self.hub_url}/set_message_id",
                    json={"task_id": task_id, "message_id": message_id},
                )
        except Exception as e:
            logger.error(f"Failed to set message_id: {e}")

    async def _flush_buffer(self, chat_id: int):
        await asyncio.sleep(MESSAGE_BATCH_DELAY)

        buffer = self._buffers.pop(chat_id, [])
        self._buffer_timers.pop(chat_id, None)

        if not buffer:
            return

        merged_text = "\n".join(text for text, _ in buffer)

        try:
            await self._ws_send_dispatch(merged_text, chat_id)
        except Exception as e:
            logger.error(f"Error dispatching merged message: {e}")
            last_event = buffer[-1][1]
            await last_event.reply(f"處理失敗: {type(e).__name__}: {e}")

    def _setup_handlers(self):
        @self.client.on(events.NewMessage)
        async def handler(event):
            if event.out:
                return
            if self.allowed_chats and event.chat_id not in self.allowed_chats:
                return
            if not event.text:
                return

            chat_id = event.chat_id
            message = event.text

            if event.reply_to and event.reply_to.reply_to_msg_id:
                reply_to_message_id = event.reply_to.reply_to_msg_id
                try:
                    await self._ws_send_dispatch(message, chat_id, reply_to_message_id)
                except Exception as e:
                    logger.error(f"Error dispatching reply: {e}")
                    await event.reply(f"處理失敗: {type(e).__name__}: {e}")
                return

            if chat_id not in self._buffers:
                self._buffers[chat_id] = []

            self._buffers[chat_id].append((message, event))

            if chat_id in self._buffer_timers:
                self._buffer_timers[chat_id].cancel()

            self._buffer_timers[chat_id] = asyncio.create_task(
                self._flush_buffer(chat_id)
            )

    async def _ws_loop(self):
        """Maintain WS connection to Hub. Auto-reconnect."""
        ws_url = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/gateway"

        while True:
            try:
                async with ClientSession() as session:
                    async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
                        self._ws = ws
                        logger.info(f"WS connected to Hub: {ws_url}")

                        # Register gateway info
                        phone_masked = self.phone[:4] + "***" + self.phone[-3:] if self.phone else None
                        await ws.send_json({
                            "type": MsgType.GW_REGISTER.value,
                            "mode": "userbot",
                            "phone": phone_masked,
                            "allowed_chats": self.allowed_chats,
                        })

                        async for msg in ws:
                            if msg.type == WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                await self._handle_hub_message(data)
                            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                                break

            except Exception as e:
                logger.warning(f"WS connection lost: {e}")

            self._ws = None
            logger.info("Reconnecting to Hub in 3 seconds...")
            await asyncio.sleep(3)

    async def run_async(self):
        """Async entry point: start Telethon + WS concurrently."""
        self._setup_handlers()
        await self.client.start(phone=self.phone)
        logger.info("Telegram Userbot connected")

        # Run WS loop and Telethon concurrently
        await asyncio.gather(
            self._ws_loop(),
            self.client.run_until_disconnected(),
        )

    def run(self):
        self._setup_handlers()
        logger.info("Telegram Userbot starting...")
        self.client.start(phone=self.phone)
        logger.info("Telegram Userbot connected")

        # Start WS loop as background task
        loop = self.client.loop
        loop.create_task(self._ws_loop())

        self.client.run_until_disconnected()
```

- [ ] **Step 2: Update `telegram_handler.py` similarly**

```python
# gateway/telegram_handler.py
import os
import json
import asyncio
import logging
from aiohttp import ClientSession, WSMsgType
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from core.ws import MsgType

logger = logging.getLogger(__name__)


class TelegramHandler:
    def __init__(self, token: str, hub_url: str):
        self.token = token
        self.hub_url = hub_url
        self._ws = None
        self._pending_replies: dict[str, asyncio.Future] = {}  # task_id → future for response

    async def _ws_send_dispatch(self, message: str, chat_id: int) -> dict | None:
        """Send dispatch via WS and wait for reply."""
        if not self._ws or self._ws.closed:
            return {"status": "error", "message": "Hub 未連線"}
        await self._ws.send_json({
            "type": MsgType.DISPATCH.value,
            "message": message,
            "chat_id": chat_id,
        })
        return None  # Reply comes asynchronously via WS

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Agent Platform 已啟動！直接輸入訊息，我會分配給對應的 agent 處理。"
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message.text
        chat_id = update.effective_chat.id
        await self._ws_send_dispatch(message, chat_id)
        # Reply will come via WS _handle_hub_message

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = query.data
        chat_id = update.effective_chat.id
        await self._ws_send_dispatch(choice, chat_id)

    async def _handle_hub_message(self, data: dict, app: Application):
        """Handle reply/progress from Hub via WS."""
        chat_id = data.get("chat_id")
        text = data.get("message", "")
        status = data.get("status")
        options = data.get("options")

        if not chat_id or not text:
            return

        if status == "need_approval":
            text = f"⚠️ {text}"

        try:
            bot = app.bot
            if options:
                keyboard = [[InlineKeyboardButton(opt, callback_data=opt)] for opt in options]
                await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await bot.send_message(chat_id, text)
        except Exception as e:
            logger.error(f"Failed to send TG message: {e}")

    async def _ws_loop(self, app: Application):
        """Maintain WS connection to Hub."""
        ws_url = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/gateway"

        while True:
            try:
                async with ClientSession() as session:
                    async with session.ws_connect(ws_url, heartbeat=20.0) as ws:
                        self._ws = ws
                        logger.info(f"WS connected to Hub: {ws_url}")

                        await ws.send_json({
                            "type": MsgType.GW_REGISTER.value,
                            "mode": "bot",
                        })

                        async for msg in ws:
                            if msg.type == WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                await self._handle_hub_message(data, app)
                            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                                break
            except Exception as e:
                logger.warning(f"WS connection lost: {e}")

            self._ws = None
            await asyncio.sleep(3)

    def create_application(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        return app

    def run(self):
        app = self.create_application()
        logger.info("Telegram Handler started")

        # Start WS loop as post_init
        async def post_init(application):
            asyncio.create_task(self._ws_loop(application))

        app.post_init = post_init
        app.run_polling()
```

- [ ] **Step 3: Commit**

```bash
git add gateway/telegram_user_handler.py gateway/telegram_handler.py
git commit -m "feat(gateway): WS client to Hub, replace HTTP dispatch"
```

---

## Task 8: Hub Dashboard 更新（Gateway 連線 + Task 終止 WS）

**Files:**
- Modify: `hub/dashboard.py`
- Modify: `hub/server.py` (task close 走 WS cancel)

- [ ] **Step 1: Add Gateway section to dashboard HTML**

修改 `hub/dashboard.py` 的 `DASHBOARD_HTML`。在 stats div 後面，agents section 前面加入 Gateway section：

在 JS `loadAll` 函式裡加入 `loadGateways()`。新增 `loadGateways` 函式：

```javascript
        async function loadGateways() {
            const res = await fetch('/dashboard/gateways');
            const data = await res.json();
            const gwSection = document.getElementById('gateways');
            if (!data.gateways.length) {
                gwSection.innerHTML = '<div class="empty">沒有 Gateway 連線</div>';
                return;
            }
            gwSection.innerHTML = data.gateways.map(g => `
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">Gateway (${g.mode || 'unknown'})</span>
                        <span class="badge badge-online">已連線</span>
                    </div>
                    <div class="meta">
                        ${g.phone ? `<span class="meta-item">📱 ${g.phone}</span>` : ''}
                        ${g.allowed_chats ? `<span class="meta-item">💬 ${g.allowed_chats.length} 個群組</span>` : ''}
                    </div>
                </div>
            `).join('');
        }
```

- [ ] **Step 2: Add `/dashboard/gateways` endpoint in `hub/server.py`**

```python
async def handle_dashboard_gateways(request: web.Request) -> web.Response:
    gateways = request.app.get("gateway_connections", [])
    result = []
    for gw in gateways:
        if gw.get("ws") and not gw["ws"].closed:
            result.append({
                "mode": gw.get("mode"),
                "phone": gw.get("phone"),
                "allowed_chats": gw.get("allowed_chats"),
            })
    return web.json_response({"gateways": result})
```

掛載路由：
```python
    app.router.add_get("/dashboard/gateways", handle_dashboard_gateways)
```

- [ ] **Step 3: Modify task close to send WS cancel**

修改 `hub/dashboard.py` 的 `handle_task_close`：

```python
async def handle_task_close(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    task_manager = request.app["task_manager"]
    task = task_manager.get_task(task_id)

    if task and task["status"] in ("working", "waiting_input", "waiting_approval"):
        # Send cancel to agent via WS
        registry = request.app["registry"]
        agent_ws = registry.get_ws(task["agent_name"])
        if agent_ws:
            from core.ws import ws_msg, MsgType
            await agent_ws.send_str(ws_msg(MsgType.CANCEL, task_id=task_id))

    task_manager.close_task(task_id)
    return web.json_response({"status": "ok"})
```

- [ ] **Step 4: Update dashboard stats to include Gateway count**

在 `loadAll` 的 stats HTML 裡加入 Gateway 連線數。

- [ ] **Step 5: Commit**

```bash
git add hub/dashboard.py hub/server.py
git commit -m "feat(hub): dashboard shows gateway connections + task cancel via WS"
```

---

## Task 9: tg-transfer WS 整合（batch 非阻塞 + progress + cancel）

**Files:**
- Modify: `agents/tg_transfer/__main__.py`
- Modify: `agents/tg_transfer/transfer_engine.py`

- [ ] **Step 1: Add cancel check to TransferEngine.run_batch**

修改 `agents/tg_transfer/transfer_engine.py` 的 `run_batch` 方法，在 while loop 開頭加入 cancel 檢查：

在 `TransferEngine.__init__` 加入：
```python
        self._cancelled: set[str] = set()
```

新增方法：
```python
    def cancel_job(self, job_id: str):
        self._cancelled.add(job_id)
```

在 `run_batch` 的 `while True:` 開頭加入：
```python
            # Check cancel
            if job_id in self._cancelled:
                self._cancelled.discard(job_id)
                await self.db.update_job_status(job_id, "cancelled")
                return "cancelled"
```

- [ ] **Step 2: Modify `_start_batch` and `_resume_batch` to be non-blocking**

修改 `agents/tg_transfer/__main__.py`：

```python
    async def _start_batch(self, task_id: str, job_id: str, job: dict) -> AgentResult:
        """Populate job_messages and start batch transfer in event loop."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])

        filter_type = job["filter_type"] or "all"
        filter_value = json.loads(job["filter_value"]) if job["filter_value"] else None
        messages = await self._collect_messages(source_entity, filter_type, filter_value)

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

        # Store chat_id for progress reporting
        chat_id = self._current_chat_id.get(task_id, 0)

        # Start batch in event loop (non-blocking)
        asyncio.create_task(
            self._run_batch_background(task_id, job_id, job, source_entity, target_entity, chat_id)
        )

        return AgentResult(
            status=TaskStatus.DONE,
            message=f"開始搬移 {len(msg_ids)} 則訊息\n來源：{job['source_chat']}\n目標：{job['target_chat']}",
        )

    async def _run_batch_background(self, task_id: str, job_id: str, job: dict,
                                     source_entity, target_entity, chat_id: int):
        """Run batch transfer and report via WS."""
        async def report_fn(text):
            await self.ws_send_progress(task_id, chat_id, text)

        try:
            status = await self.engine.run_batch(job_id, source_entity, target_entity, report_fn)

            progress = await self.db.get_progress(job_id)

            if status == "paused":
                await self.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.NEED_INPUT,
                    message=f"搬移暫停\n"
                            f"進度：{progress['success']}/{progress['total']}\n"
                            f"請選擇：重試 / 跳過 / 一律跳過",
                ))
            elif status == "cancelled":
                await self.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.DONE,
                    message=f"搬移已取消\n"
                            f"成功：{progress['success']} 則\n"
                            f"跳過：{progress['skipped']} 則",
                ))
            else:
                await self.ws_send_result(task_id, AgentResult(
                    status=TaskStatus.DONE,
                    message=f"搬移完成\n"
                            f"來源：{job['source_chat']}\n"
                            f"目標：{job['target_chat']}\n"
                            f"成功：{progress['success']} 則\n"
                            f"跳過：{progress['skipped']} 則\n"
                            f"失敗：{progress['failed']} 則",
                ))
        except Exception as e:
            logger.error(f"Batch transfer error: {e}", exc_info=True)
            await self.ws_send_result(task_id, AgentResult(
                status=TaskStatus.ERROR,
                message=f"搬移失敗：{e}",
            ))
        finally:
            self._pending_jobs.pop(task_id, None)

    async def _resume_batch(self, task_id: str, job_id: str, job: dict) -> AgentResult:
        """Resume a paused batch job (non-blocking)."""
        source_entity = await resolve_chat(self.tg_client, job["source_chat"])
        target_entity = await resolve_chat(self.tg_client, job["target_chat"])
        chat_id = self._current_chat_id.get(task_id, 0)

        asyncio.create_task(
            self._run_batch_background(task_id, job_id, job, source_entity, target_entity, chat_id)
        )

        return AgentResult(
            status=TaskStatus.DONE,
            message="繼續搬移中...",
        )
```

在 `__init__` 加入：
```python
        self._current_chat_id: dict[str, int] = {}  # task_id → chat_id
```

在 `handle_task` 裡記錄 chat_id：
```python
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        if self._init_error:
            return AgentResult(
                status=TaskStatus.ERROR,
                message=f"Agent 初始化失敗，無法處理任務：{self._init_error}",
            )
        self._current_chat_id[task.task_id] = task.chat_id
        try:
            return await self._dispatch(task)
        except Exception as e:
            logger.error(f"handle_task error: {e}", exc_info=True)
            return AgentResult(status=TaskStatus.ERROR, message=f"執行失敗：{e}")
```

- [ ] **Step 3: Wire cancel from WS to TransferEngine**

在 `TGTransferAgent` 覆寫 BaseAgent 的 cancel handling。在 `__init__` 後面加入：

```python
    async def _handle_ws_task(self, data: dict):
        """Override to handle cancel for batch jobs."""
        msg_type = data.get("type")
        if msg_type == "cancel":
            task_id = data.get("task_id")
            if task_id and task_id in self._pending_jobs:
                job_id = self._pending_jobs[task_id]
                self.engine.cancel_job(job_id)
            return
        await super()._handle_ws_task(data)
```

注意：這需要在 BaseAgent 的 `_ws_loop` 中，cancel 的處理要能被子類覆寫。但目前 BaseAgent 已經在 `_ws_loop` 裡處理了 cancel（加入 `_cancelled_tasks`）。tg-transfer 需要額外把 cancel 傳給 TransferEngine。

改為在 `_handle_ws_task` 中處理（BaseAgent 已經有 `_handle_ws_task`）。修改 tg-transfer 的 `handle_task` 不需要變，但需要讓 `_ws_loop` 裡的 cancel 也通知到 engine。

最簡潔的做法：override `_handle_ws_task` 以攔截 cancel 訊息，不呼叫 super 的 cancel 邏輯：

不對，BaseAgent 的 `_ws_loop` 裡已經分開處理了 cancel 和 task。我們在 tg-transfer 裡覆寫 BaseAgent，加一個 hook：

在 BaseAgent 加入：
```python
    def on_cancel(self, task_id: str):
        """Hook for subclasses to handle task cancellation."""
        pass
```

在 `_ws_loop` 的 cancel 處理裡呼叫：
```python
                                elif msg_type == MsgType.CANCEL.value:
                                    task_id = data.get("task_id")
                                    if task_id:
                                        self._cancelled_tasks.add(task_id)
                                        self.on_cancel(task_id)
                                        logger.info(f"Task cancelled: {task_id}")
```

在 TGTransferAgent 覆寫：
```python
    def on_cancel(self, task_id: str):
        if task_id in self._pending_jobs:
            job_id = self._pending_jobs[task_id]
            self.engine.cancel_job(job_id)
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/ -v --ignore=tests/test_integration.py --ignore=tests/test_tg_transfer_integration.py`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tg_transfer/__main__.py agents/tg_transfer/transfer_engine.py core/base_agent.py
git commit -m "feat(tg-transfer): non-blocking batch + WS progress + cancel support"
```

---

## Task 10: 清理舊 HTTP 程式碼

**Files:**
- Modify: `hub/server.py` (移除舊 dispatch/heartbeat handler)
- Modify: `hub/cli.py` (移除 send_task_to_agent)

- [ ] **Step 1: Remove HTTP /heartbeat endpoint from server.py**

從 `hub/server.py` 移除：
- `handle_heartbeat` 函式
- `app.router.add_post("/heartbeat", handle_heartbeat)` 路由

- [ ] **Step 2: Remove send_task_to_agent from hub/cli.py**

從 `hub/cli.py` 移除 `send_task_to_agent` 函式。保留 `cli_loop` 和 `dispatch_message`（如果還需要 CLI 模式的話，需要改為走 WS）。

如果 CLI 模式不再使用，可以移除整個 `dispatch_message`。保留 `cli_loop` 改為用 WS。

- [ ] **Step 3: Remove old HTTP dispatch handler if fully replaced**

如果所有 Gateway 都已改為 WS，可以移除 `handle_dispatch` 和相關 helper（`_continue_task`, `_dispatch_to_agent`, `_hub_chat_reply`, `_update_task_status`, `_get_all_active_tasks`）。

但為了向後相容（如有其他 client），可暫時保留 `/dispatch` 作為 fallback。

- [ ] **Step 4: Run all tests**

Run: `cd /Users/edgar_cheng/Desktop/workspaces/agent && python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add hub/server.py hub/cli.py
git commit -m "refactor(hub): remove HTTP heartbeat, clean up old dispatch code"
```

---

## Task 11: 更新 Dashboard agent 狀態顯示

**Files:**
- Modify: `hub/registry.py` (list_all 加入 WS 狀態)
- Modify: `hub/dashboard.py` (顯示 WS connected 取代 heartbeat)

- [ ] **Step 1: Update `list_all` in registry to include ws_connected**

在 `hub/registry.py` 的 `list_all` 方法中，`result.append` 的 dict 加入：
```python
                "ws_connected": self.has_ws(name),
```

agent status 判斷修改：
```python
            if name in self._errors:
                status = "error"
            elif name in self._unauthenticated:
                status = "unauthenticated"
            elif disabled:
                status = "disabled"
            elif self.has_ws(name):
                status = "online"
            else:
                status = "offline"
```

- [ ] **Step 2: Update dashboard HTML to show WS status**

在 agent stats 區塊，將「最後心跳」改為「WS 連線」：

```javascript
<div class="agent-stat"><span class="agent-stat-label">WS:</span> <span class="agent-stat-value">${a.ws_connected ? '已連線' : '未連線'}</span></div>
```

- [ ] **Step 3: Add Gateway count to top stats**

```javascript
// In loadAll
const gwRes = await fetch('/dashboard/gateways');
const gwData = await gwRes.json();
// Add to stats HTML:
<div class="stat"><div class="stat-value">${gwData.gateways.length}</div><div class="stat-label">Gateway 連線</div></div>
```

- [ ] **Step 4: Commit**

```bash
git add hub/registry.py hub/dashboard.py
git commit -m "feat(hub): dashboard shows WS connection status + gateway count"
```
