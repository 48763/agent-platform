# Shared Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade core shared modules so new agents only need business logic: unified LLM client (Claude/Gemini), shared dashboard framework, and agent error reporting to Hub.

**Architecture:** `core/llm.py` becomes a provider-based LLM client factory. `core/agent_dashboard.py` renders stats dicts to HTML. Hub gets a `/register_error` endpoint and error state in registry. BaseAgent validates LLM on startup.

**Tech Stack:** Python 3.12, aiohttp, anthropic SDK, Gemini CLI

---

## File Structure

```
core/
├── llm.py              # REWRITE: unified LLM interface with Claude/Gemini providers
├── agent_dashboard.py  # NEW: shared dashboard HTML renderer
├── base_agent.py       # MODIFY: add LLM init + _register_error

hub/
├── server.py           # MODIFY: add /register_error route
├── registry.py         # MODIFY: add error state + register_error method
├── dashboard.py        # MODIFY: render error state agents

agents/tg_transfer/
├── __main__.py         # MODIFY: use core LLM client + core dashboard
├── dashboard.py        # MODIFY: use core/agent_dashboard.py

tests/
├── test_llm.py         # REWRITE: test both providers + config parsing
├── test_agent_dashboard.py  # NEW: test HTML rendering
├── test_registry.py    # MODIFY: test error state
```

---

### Task 1: core/llm.py — Unified LLM Client

**Files:**
- Rewrite: `core/llm.py`
- Rewrite: `tests/test_llm.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_llm.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.llm import parse_llm_config, LLMClient, LLMInitError, ClaudeProvider, GeminiProvider


class TestParseLLMConfig:
    def test_string_claude(self):
        provider, model = parse_llm_config({"llm": "claude"})
        assert provider == "claude"
        assert "claude" in model

    def test_string_gemini(self):
        provider, model = parse_llm_config({"llm": "gemini"})
        assert provider == "gemini"
        assert "gemini" in model

    def test_dict_with_model(self):
        provider, model = parse_llm_config({"llm": {"provider": "claude", "model": "claude-haiku-4-5-20251001"}})
        assert provider == "claude"
        assert model == "claude-haiku-4-5-20251001"

    def test_dict_without_model_uses_default(self):
        provider, model = parse_llm_config({"llm": {"provider": "gemini"}})
        assert provider == "gemini"
        assert "gemini" in model

    def test_no_llm_config(self):
        provider, model = parse_llm_config({})
        assert provider is None
        assert model is None

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError):
            parse_llm_config({"llm": "openai"})


class TestClaudeProvider:
    @pytest.mark.asyncio
    async def test_prompt(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="hello")]
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        provider = ClaudeProvider(client=mock_client, model="test-model")
        result = await provider.prompt("say hi")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_run_agentic_loop(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="done")]
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        provider = ClaudeProvider(client=mock_client, model="test-model")
        result = await provider.run(
            system_prompt="test", messages=[{"role": "user", "content": "hi"}],
            tools_schema=[], tool_executor=None,
        )
        assert result == "done"


class TestGeminiProvider:
    @pytest.mark.asyncio
    async def test_prompt(self):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"hello world", b""))
            proc.returncode = 0
            mock_exec.return_value = proc

            provider = GeminiProvider(model="gemini-2.5-flash")
            result = await provider.prompt("say hi")
            assert result == "hello world"

    @pytest.mark.asyncio
    async def test_run_raises_not_implemented(self):
        provider = GeminiProvider(model="gemini-2.5-flash")
        with pytest.raises(NotImplementedError):
            await provider.run(
                system_prompt="test", messages=[], tools_schema=[], tool_executor=None,
            )


class TestLLMClient:
    @pytest.mark.asyncio
    async def test_prompt_delegates_to_provider(self):
        mock_provider = AsyncMock()
        mock_provider.prompt = AsyncMock(return_value="result")
        client = LLMClient(provider=mock_provider)
        result = await client.prompt("test")
        assert result == "result"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_llm.py -v`
Expected: FAIL

- [ ] **Step 3: Implement core/llm.py**

```python
# core/llm.py
import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "claude": "claude-sonnet-4-20250514",
    "gemini": "gemini-2.5-flash",
}


class LLMInitError(Exception):
    """Raised when LLM provider fails to initialize."""
    pass


def parse_llm_config(settings: dict) -> tuple[Optional[str], Optional[str]]:
    """Parse llm config from agent.yaml settings. Returns (provider, model)."""
    llm = settings.get("llm")
    if llm is None:
        return None, None
    if isinstance(llm, str):
        if llm not in DEFAULT_MODELS:
            raise ValueError(f"Unknown LLM provider: {llm}. Supported: {list(DEFAULT_MODELS.keys())}")
        return llm, DEFAULT_MODELS[llm]
    if isinstance(llm, dict):
        provider = llm["provider"]
        if provider not in DEFAULT_MODELS:
            raise ValueError(f"Unknown LLM provider: {provider}. Supported: {list(DEFAULT_MODELS.keys())}")
        model = llm.get("model", DEFAULT_MODELS[provider])
        return provider, model
    return None, None


class ClaudeProvider:
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    async def prompt(self, text: str) -> str:
        """Single prompt → response."""
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": text}],
        )
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    async def run(
        self,
        system_prompt: str,
        messages: list[dict],
        tools_schema: list[dict],
        tool_executor: Optional[Callable],
        max_iterations: int = 20,
    ) -> str:
        """Agentic loop with tool calling."""
        messages = list(messages)
        for _ in range(max_iterations):
            kwargs = {
                "model": self.model,
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": messages,
            }
            if tools_schema:
                kwargs["tools"] = tools_schema
            response = await self.client.messages.create(**kwargs)
            if response.stop_reason == "tool_use":
                tool_calls = [b for b in response.content if b.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for tc in tool_calls:
                    result = await tool_executor(tc.name, tc.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": str(result),
                    })
                messages.append({"role": "user", "content": tool_results})
            else:
                for block in response.content:
                    if block.type == "text":
                        return block.text
                return ""
        return "Error: max iterations reached"


class GeminiProvider:
    def __init__(self, model: str):
        self.model = model

    async def prompt(self, text: str) -> str:
        """Single prompt → response via Gemini CLI."""
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", text, "-m", self.model,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.error(f"Gemini CLI error: {stderr.decode()}")
        return stdout.decode().strip()

    async def run(self, system_prompt, messages, tools_schema, tool_executor, max_iterations=20):
        """Not supported for Gemini CLI."""
        raise NotImplementedError("Gemini provider does not support agentic loop with tool calling")


class LLMClient:
    def __init__(self, provider: ClaudeProvider | GeminiProvider):
        self.provider = provider

    async def prompt(self, text: str) -> str:
        return await self.provider.prompt(text)

    async def run(self, system_prompt, messages, tools_schema, tool_executor, max_iterations=20) -> str:
        return await self.provider.run(
            system_prompt, messages, tools_schema, tool_executor, max_iterations,
        )


async def create_llm_client(settings: dict) -> LLMClient:
    """Factory: create LLMClient from agent.yaml settings. Raises LLMInitError on failure."""
    provider_name, model = parse_llm_config(settings)
    if provider_name is None:
        raise LLMInitError("No LLM configured in agent.yaml settings")

    if provider_name == "claude":
        try:
            import anthropic
            import os
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise LLMInitError("Claude API key 未設定：請設定 ANTHROPIC_API_KEY 環境變數")
            client = anthropic.AsyncAnthropic()
            return LLMClient(provider=ClaudeProvider(client=client, model=model))
        except ImportError:
            raise LLMInitError("anthropic SDK 未安裝")
        except Exception as e:
            raise LLMInitError(f"Claude 初始化失敗：{e}")

    if provider_name == "gemini":
        try:
            proc = await asyncio.create_subprocess_exec(
                "which", "gemini",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                raise LLMInitError("Gemini CLI 未安裝：找不到 gemini 指令")
            # Test with a simple prompt
            proc = await asyncio.create_subprocess_exec(
                "gemini", "-p", "ping", "-m", model,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                raise LLMInitError(f"Gemini CLI 測試失敗：{stderr.decode().strip()}")
            return LLMClient(provider=GeminiProvider(model=model))
        except LLMInitError:
            raise
        except Exception as e:
            raise LLMInitError(f"Gemini 初始化失敗：{e}")

    raise LLMInitError(f"Unknown provider: {provider_name}")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_llm.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/llm.py tests/test_llm.py
git commit -m "feat(core): rewrite llm.py with unified Claude/Gemini provider interface"
```

---

### Task 2: core/agent_dashboard.py — Shared Dashboard Framework

**Files:**
- Create: `core/agent_dashboard.py`
- Create: `tests/test_agent_dashboard.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_agent_dashboard.py
import pytest
from aiohttp import web
from core.agent_dashboard import create_dashboard_handler, render_dashboard_html


class TestRenderDashboardHTML:
    def test_renders_title(self):
        stats = {"title": "My Stats", "counters": [], "tables": []}
        html = render_dashboard_html(stats)
        assert "My Stats" in html

    def test_renders_counters(self):
        stats = {
            "title": "Test",
            "counters": [("媒體數", 42), ("標籤", 5)],
            "tables": [],
        }
        html = render_dashboard_html(stats)
        assert "42" in html
        assert "媒體數" in html
        assert "5" in html

    def test_renders_table(self):
        stats = {
            "title": "Test",
            "counters": [],
            "tables": [{
                "title": "標籤統計",
                "headers": ["標籤", "數量"],
                "rows": [("#教學", 10), ("#python", 5)],
            }],
        }
        html = render_dashboard_html(stats)
        assert "標籤統計" in html
        assert "#教學" in html
        assert "10" in html

    def test_empty_tables(self):
        stats = {"title": "Empty", "counters": [], "tables": []}
        html = render_dashboard_html(stats)
        assert "Empty" in html

    def test_multiple_tables(self):
        stats = {
            "title": "Multi",
            "counters": [],
            "tables": [
                {"title": "T1", "headers": ["A"], "rows": [("x",)]},
                {"title": "T2", "headers": ["B"], "rows": [("y",)]},
            ],
        }
        html = render_dashboard_html(stats)
        assert "T1" in html
        assert "T2" in html


class TestCreateDashboardHandler:
    @pytest.mark.asyncio
    async def test_handler_returns_html(self, aiohttp_client):
        async def my_stats():
            return {"title": "Test", "counters": [("Count", 1)], "tables": []}

        app = web.Application()
        app.router.add_get("/dashboard", create_dashboard_handler(my_stats))
        client = await aiohttp_client(app)
        resp = await client.get("/dashboard")
        assert resp.status == 200
        text = await resp.text()
        assert "Test" in text
        assert "text/html" in resp.headers["Content-Type"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_agent_dashboard.py -v`
Expected: FAIL

- [ ] **Step 3: Implement core/agent_dashboard.py**

```python
# core/agent_dashboard.py
from aiohttp import web
from typing import Callable


def render_dashboard_html(stats: dict) -> str:
    """Render a stats dict into an HTML page."""
    title = stats.get("title", "Agent Dashboard")
    counters = stats.get("counters", [])
    tables = stats.get("tables", [])

    counter_html = ""
    for label, value in counters:
        counter_html += (
            f'<div class="stat-card">'
            f'<div class="stat-value">{value}</div>'
            f'<div class="stat-label">{label}</div>'
            f'</div>\n'
        )

    tables_html = ""
    for table in tables:
        headers = "".join(f"<th>{h}</th>" for h in table.get("headers", []))
        rows = ""
        for row in table.get("rows", []):
            cells = "".join(f"<td>{c}</td>" for c in row)
            rows += f"<tr>{cells}</tr>\n"
        tables_html += (
            f'<h2>{table.get("title", "")}</h2>\n'
            f'<table><tr>{headers}</tr>\n{rows}</table>\n'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
       background: #0d1117; color: #c9d1d9; max-width: 700px; margin: 40px auto; padding: 0 20px; }}
h1 {{ color: #58a6ff; margin-bottom: 20px; }}
h2 {{ color: #8b949e; margin: 24px 0 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; }}
.stats-row {{ display: flex; gap: 24px; margin: 20px 0; flex-wrap: wrap; }}
.stat-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px 24px; }}
.stat-value {{ font-size: 2em; font-weight: bold; color: #f0f6fc; }}
.stat-label {{ color: #8b949e; font-size: 0.9em; margin-top: 4px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
th, td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: left; }}
th {{ background: #161b22; color: #8b949e; }}
td {{ color: #c9d1d9; }}
tr:hover {{ background: #161b22; }}
</style></head>
<body>
<h1>{title}</h1>
<div class="stats-row">
{counter_html if counter_html else '<div class="stat-card"><div class="stat-label">尚無資料</div></div>'}
</div>
{tables_html}
</body></html>"""


def create_dashboard_handler(stats_fn: Callable) -> Callable:
    """Create an aiohttp handler that renders stats from the given async function."""
    async def handler(request: web.Request) -> web.Response:
        stats = await stats_fn()
        html = render_dashboard_html(stats)
        return web.Response(text=html, content_type="text/html")
    return handler
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_agent_dashboard.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/agent_dashboard.py tests/test_agent_dashboard.py
git commit -m "feat(core): add shared dashboard framework with HTML rendering"
```

---

### Task 3: Hub /register_error + Registry Error State

**Files:**
- Modify: `hub/registry.py`
- Modify: `hub/server.py`
- Modify: `tests/test_registry.py`

- [ ] **Step 1: Write failing tests for registry error state**

Add to `tests/test_registry.py`:

```python
# Add these tests to the existing test file

def test_register_error(registry):
    registry.register_error("broken-agent", "LLM 不可用：找不到 gemini CLI")
    agents = registry.list_all()
    broken = [a for a in agents if a["name"] == "broken-agent"]
    assert len(broken) == 1
    assert broken[0]["status"] == "error"
    assert broken[0]["error"] == "LLM 不可用：找不到 gemini CLI"


def test_register_clears_error(registry):
    from core.models import AgentInfo
    registry.register_error("recover-agent", "some error")
    # Now register normally
    info = AgentInfo(name="recover-agent", description="test", url="http://localhost:8000")
    registry.register(info)
    agents = registry.list_all()
    agent = [a for a in agents if a["name"] == "recover-agent"][0]
    assert agent["status"] == "online"
    assert agent.get("error") is None


def test_error_agent_not_in_online_list(registry):
    registry.register_error("err-agent", "broken")
    online = registry.list_online()
    names = [a.name for a in online]
    assert "err-agent" not in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_registry.py -v -k "error"`
Expected: FAIL

- [ ] **Step 3: Add register_error to registry.py**

Add `_errors` dict and `register_error` method to `AgentRegistry`:

In `__init__`, add:
```python
        self._errors: dict[str, str] = {}  # name → error message
```

Add method:
```python
    def register_error(self, name: str, error: str) -> None:
        """Record an agent that failed to start."""
        self._errors[name] = error
```

Modify `register` to clear error state:
```python
    def register(self, info: AgentInfo) -> None:
        self._agents[info.name] = info
        self._last_heartbeat[info.name] = time.time()
        self._errors.pop(info.name, None)  # Clear any previous error
        if info.name not in self._registered_at:
            self._registered_at[info.name] = time.time()
        if info.name not in self._stats:
            self._stats[info.name] = {"tasks": 0, "success": 0, "errors": 0, "total_ms": 0}
```

Modify `list_all` to include error agents — in the status determination block, add error check before disabled/alive:
```python
            if name in self._errors:
                status = "error"
            elif disabled:
                status = "disabled"
            elif alive:
                status = "online"
            else:
                status = "offline"

            result.append({
                **info.to_dict(),
                "status": status,
                "error": self._errors.get(name),
                "last_heartbeat": last_hb,
                ...
            })
```

Also add error-only agents (not in `_agents` but in `_errors`):
```python
        # Add agents that only have error state (never successfully registered)
        for name, error in self._errors.items():
            if name not in self._agents:
                result.append({
                    "name": name,
                    "description": "",
                    "url": "",
                    "route_patterns": [],
                    "capabilities": [],
                    "priority": 0,
                    "status": "error",
                    "error": error,
                    "last_heartbeat": 0,
                    "registered_at": 0,
                    "uptime_seconds": 0,
                    "stats": {"total_tasks": 0, "success": 0, "errors": 0, "avg_response_ms": 0},
                })
```

- [ ] **Step 4: Add /register_error route to server.py**

Add handler function:
```python
async def handle_register_error(request: web.Request) -> web.Response:
    data = await request.json()
    name = data.get("name", "unknown")
    error = data.get("error", "unknown error")
    request.app["registry"].register_error(name, error)
    return web.json_response({"status": "recorded", "name": name})
```

Add route in `create_hub_app` after `/register`:
```python
    app.router.add_post("/register_error", handle_register_error)
```

Add `/register_error` to `no_auth_prefixes` in middleware:
```python
        no_auth_prefixes = ("/register", "/heartbeat", "/agents", "/dispatch", "/set_message_id", "/auth/")
```
(Already covered by `/register` prefix match.)

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_registry.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add hub/registry.py hub/server.py tests/test_registry.py
git commit -m "feat(hub): add /register_error endpoint and error state for agents"
```

---

### Task 4: BaseAgent — LLM Init + Error Reporting

**Files:**
- Modify: `core/base_agent.py`

- [ ] **Step 1: Update base_agent.py**

Add imports:
```python
import sys
from core.llm import create_llm_client, LLMInitError, LLMClient
```

Add `self.llm: LLMClient | None = None` to `__init__`.

Add `_register_error` method:
```python
    async def _register_error(self, error: str) -> None:
        """Report startup error to Hub."""
        try:
            async with ClientSession() as session:
                await session.post(
                    f"{self.hub_url}/register_error",
                    json={"name": self.name, "error": error},
                )
        except Exception:
            pass  # Hub might not be running
```

Modify `run()` to validate LLM before starting:
```python
    async def run(self) -> None:
        # Validate LLM if configured
        settings = self.config.get("settings", {})
        if settings.get("llm"):
            try:
                self.llm = await create_llm_client(settings)
            except LLMInitError as e:
                await self._register_error(str(e))
                print(f"ERROR: LLM init failed: {e}", file=sys.stderr)
                sys.exit(1)

        app = self.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()

        actual_port = site._server.sockets[0].getsockname()[1]
        print(f"Agent '{self.name}' running on port {actual_port}")

        await self.register(actual_port)
        await self._heartbeat_loop(actual_port)
```

- [ ] **Step 2: Commit**

```bash
git add core/base_agent.py
git commit -m "feat(core): add LLM validation and error reporting to BaseAgent"
```

---

### Task 5: Migrate TG Transfer Agent to Shared Modules

**Files:**
- Modify: `agents/tg_transfer/__main__.py`
- Modify: `agents/tg_transfer/dashboard.py`

- [ ] **Step 1: Update dashboard.py to use shared framework**

Replace entire `agents/tg_transfer/dashboard.py`:

```python
# agents/tg_transfer/dashboard.py
from core.agent_dashboard import create_dashboard_handler
from agents.tg_transfer.media_db import MediaDB


def create_tg_dashboard_handler(media_db: MediaDB):
    """Create dashboard handler using shared framework."""
    async def get_stats():
        stats = await media_db.get_stats()
        return {
            "title": "TG Transfer 統計",
            "counters": [
                ("儲存媒體", stats["total_media"]),
                ("標籤總數", stats["total_tags"]),
            ],
            "tables": [{
                "title": "標籤統計",
                "headers": ["標籤", "數量"],
                "rows": [(f"#{name}", count) for name, count in stats["tag_counts"]],
            }] if stats["tag_counts"] else [],
        }
    return create_dashboard_handler(get_stats)
```

- [ ] **Step 2: Update __main__.py — use shared LLM + dashboard**

Replace the gemini import in `_ai_parse_batch`:

Change `_ai_parse_batch` to use `self.llm`:
```python
    async def _ai_parse_batch(self, content: str) -> dict | None:
        """Use LLM to parse natural language batch command."""
        if not self.llm:
            return None
        prompt = (
            "你是一個指令解析器。從以下使用者訊息中提取搬移參數，回覆 JSON：\n"
            '{"source": "@channel 或連結", "target": "@channel 或連結 或 null", '
            '"filter_type": "all 或 count 或 date_range", '
            '"filter_value_raw": null 或 {"count": N} 或 {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}}\n\n'
            f"使用者訊息：{content}\n\n只回覆 JSON，不要解釋。"
        )
        try:
            text = await self.llm.prompt(prompt)
            # Extract JSON from response
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            return json.loads(text)
        except Exception as e:
            logger.error(f"AI parse failed: {e}")
            return None
```

Update `create_app` to use new dashboard:
```python
    def create_app(self) -> web.Application:
        app = super().create_app()
        from agents.tg_transfer.dashboard import create_tg_dashboard_handler
        app.router.add_get("/dashboard", create_tg_dashboard_handler(self.media_db))
        return app
```

Remove the old direct gemini spawn import (`asyncio.create_subprocess_exec` for gemini in `_ai_parse_batch`).

Remove the old `from agents.tg_transfer.dashboard import dashboard_handler` import, replace with nothing (dashboard is set up in `create_app`).

- [ ] **Step 3: Run all tests**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_hub_server.py --ignore=tests/test_integration.py --ignore=tests/test_dispatch.py -k "not test_cli"`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add agents/tg_transfer/__main__.py agents/tg_transfer/dashboard.py
git commit -m "refactor(tg-transfer): migrate to shared LLM client and dashboard framework"
```

---

### Task 6: Update Documentation

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`

- [ ] **Step 1: Update AGENTS.md shared modules section**

In the `LLMClient` section, update to reflect new unified interface:

```markdown
### LLMClient（Claude / Gemini 統一介面）

在 `agent.yaml` 設定使用哪個 LLM：

\`\`\`yaml
settings:
  llm: claude              # 簡寫
  # 或完整寫法：
  llm:
    provider: gemini
    model: gemini-2.5-flash
\`\`\`

Agent 啟動時自動檢測 LLM 可用性，失敗會向 Hub 回報錯誤並退出。

\`\`\`python
# BaseAgent 自動初始化 self.llm，直接使用：
result = await self.llm.prompt("翻譯這段文字")

# Claude provider 額外支援 agentic loop：
result = await self.llm.run(system_prompt="...", messages=[...], tools_schema=schema, tool_executor=fn)
\`\`\`

### Agent Dashboard（共用框架）

\`\`\`python
from core.agent_dashboard import create_dashboard_handler

async def get_stats():
    return {
        "title": "My Agent 統計",
        "counters": [("項目數", 42)],
        "tables": [{"title": "詳細", "headers": ["名稱", "值"], "rows": [("a", 1)]}],
    }

# 在 create_app 中：
app.router.add_get("/dashboard", create_dashboard_handler(get_stats))
\`\`\`
```

- [ ] **Step 2: Update README.md shared modules table**

Update the core modules table:

```markdown
| 模組 | 用途 |
|------|------|
| `base_agent.py` | Agent 基底類別：HTTP server、Hub 註冊、heartbeat、LLM 檢測 |
| `models.py` | 共用資料模型：`TaskRequest`、`AgentResult`、`TaskStatus`、`AgentInfo` |
| `config.py` | YAML 設定載入 |
| `sandbox.py` | 路徑/指令安全限制（per-agent 設定） |
| `llm.py` | 統一 LLM 介面：支援 Claude API + Gemini CLI，agent.yaml 擇一設定 |
| `tool_registry.py` | `@tool` 裝飾器 + Claude API tool schema 自動產生 |
| `agent_dashboard.py` | 共用 Dashboard 框架：agent 提供 stats dict，自動渲染 HTML |
```

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md README.md
git commit -m "docs: update shared modules documentation for LLM client and dashboard"
```
