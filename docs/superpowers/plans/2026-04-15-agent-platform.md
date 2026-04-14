# Agent Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a multi-agent platform where a Hub service receives Telegram messages, routes them to registered sub-agents via HTTP, and relays responses back — starting with a weather agent as the first functional agent.

**Architecture:** Hub (aiohttp server) manages agent registry and routes tasks. Sub-agents are independent aiohttp processes that register on startup and receive tasks via HTTP POST. Claude API powers the routing fallback and agent reasoning. CLI interface for development testing before TG integration.

**Tech Stack:** Python 3.11+, aiohttp, anthropic SDK, python-telegram-bot, pyyaml

---

## File Map

| File | Responsibility |
|------|---------------|
| `core/__init__.py` | Export core classes |
| `core/models.py` | Shared data models (AgentResult, TaskRequest, etc.) |
| `core/config.py` | YAML config loading |
| `core/tool_registry.py` | @tool decorator + schema generation |
| `core/sandbox.py` | Path/command access control |
| `core/llm.py` | Claude API wrapper + agentic loop |
| `core/base_agent.py` | BaseAgent class (HTTP server, registration, heartbeat, task handling) |
| `hub/__init__.py` | Export hub classes |
| `hub/registry.py` | Agent registry (online/offline tracking) |
| `hub/task_manager.py` | Multi-turn task state management |
| `hub/router.py` | Keyword + Claude fallback routing |
| `hub/server.py` | Hub HTTP server (register, heartbeat, dispatch endpoints) |
| `hub/cli.py` | CLI test interface |
| `hub/bot.py` | Telegram Bot integration |
| `agents/weather/agent.yaml` | Weather agent config |
| `agents/weather/tools.py` | get_weather tool (wttr.in) |
| `agents/weather/prompts.py` | Weather agent system prompt |
| `agents/weather/__main__.py` | Entry point: `python -m agents.weather` |
| `config.yaml` | Global config (hub port, API key env var) |
| `requirements.txt` | Dependencies |

---

### Task 1: Project Setup & Shared Models

**Files:**
- Create: `requirements.txt`
- Create: `config.yaml`
- Create: `core/__init__.py`
- Create: `core/models.py`
- Create: `hub/__init__.py`
- Create: `agents/__init__.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Create requirements.txt**

```
aiohttp>=3.9,<4
anthropic>=0.40,<1
python-telegram-bot>=21,<22
pyyaml>=6,<7
```

- [ ] **Step 2: Create config.yaml**

```yaml
hub:
  host: "0.0.0.0"
  port: 9000
  heartbeat_timeout: 30  # seconds

llm:
  model: "claude-sonnet-4-20250514"
```

- [ ] **Step 3: Create core/models.py with shared data models**

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid


class TaskStatus(str, Enum):
    DONE = "done"
    NEED_INPUT = "need_input"
    NEED_APPROVAL = "need_approval"
    ERROR = "error"


@dataclass
class AgentResult:
    status: TaskStatus
    message: str
    options: Optional[list[str]] = None
    action: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"status": self.status.value, "message": self.message}
        if self.options is not None:
            d["options"] = self.options
        if self.action is not None:
            d["action"] = self.action
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "AgentResult":
        return cls(
            status=TaskStatus(data["status"]),
            message=data["message"],
            options=data.get("options"),
            action=data.get("action"),
        )


@dataclass
class TaskRequest:
    task_id: str
    content: str
    conversation_history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "content": self.content,
            "conversation_history": self.conversation_history,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRequest":
        return cls(
            task_id=data["task_id"],
            content=data["content"],
            conversation_history=data.get("conversation_history", []),
        )


@dataclass
class AgentInfo:
    name: str
    description: str
    url: str
    route_patterns: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "route_patterns": self.route_patterns,
            "capabilities": self.capabilities,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentInfo":
        return cls(
            name=data["name"],
            description=data["description"],
            url=data["url"],
            route_patterns=data.get("route_patterns", []),
            capabilities=data.get("capabilities", []),
        )
```

- [ ] **Step 4: Create __init__.py files**

`core/__init__.py`:
```python
from core.models import AgentResult, TaskRequest, AgentInfo, TaskStatus
```

`hub/__init__.py`:
```python
```

`agents/__init__.py`:
```python
```

- [ ] **Step 5: Write tests for models**

```python
# tests/test_models.py
from core.models import AgentResult, TaskRequest, AgentInfo, TaskStatus


def test_agent_result_done():
    result = AgentResult(status=TaskStatus.DONE, message="完成")
    d = result.to_dict()
    assert d == {"status": "done", "message": "完成"}
    assert AgentResult.from_dict(d) == result


def test_agent_result_need_input_with_options():
    result = AgentResult(
        status=TaskStatus.NEED_INPUT,
        message="選一個",
        options=["A", "B"],
    )
    d = result.to_dict()
    assert d["options"] == ["A", "B"]
    assert AgentResult.from_dict(d) == result


def test_agent_result_need_approval():
    result = AgentResult(
        status=TaskStatus.NEED_APPROVAL,
        message="是否允許？",
        action="run_command: rm -rf dist/",
        options=["允許", "拒絕"],
    )
    d = result.to_dict()
    assert d["action"] == "run_command: rm -rf dist/"
    assert AgentResult.from_dict(d) == result


def test_task_request_roundtrip():
    req = TaskRequest(
        task_id="abc-123",
        content="台北天氣",
        conversation_history=[{"role": "user", "content": "hi"}],
    )
    d = req.to_dict()
    assert TaskRequest.from_dict(d) == req


def test_agent_info_roundtrip():
    info = AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣|weather"],
        capabilities=["get_weather"],
    )
    d = info.to_dict()
    assert AgentInfo.from_dict(d) == info
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_models.py -v`
Expected: All 5 tests PASS

- [ ] **Step 7: Setup virtual environment and install deps**

Run: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install pytest`

- [ ] **Step 8: Commit**

```bash
git add requirements.txt config.yaml core/ hub/__init__.py agents/__init__.py tests/
git commit -m "feat: project setup with shared models and config"
```

---

### Task 2: Config Loader

**Files:**
- Create: `core/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_config.py
import tempfile
import os
from pathlib import Path


def test_load_config_from_yaml(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("hub:\n  host: '0.0.0.0'\n  port: 9000\n")

    from core.config import load_config
    cfg = load_config(str(config_file))
    assert cfg["hub"]["host"] == "0.0.0.0"
    assert cfg["hub"]["port"] == 9000


def test_load_agent_config(tmp_path):
    agent_dir = tmp_path / "myagent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "name: test-agent\ndescription: test\nroute_patterns:\n  - 'test'\n"
    )

    from core.config import load_agent_config
    cfg = load_agent_config(str(agent_dir))
    assert cfg["name"] == "test-agent"
    assert cfg["route_patterns"] == ["test"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement config.py**

```python
# core/config.py
from pathlib import Path
import yaml


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_agent_config(agent_dir: str) -> dict:
    config_path = Path(agent_dir) / "agent.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/config.py tests/test_config.py
git commit -m "feat: add YAML config loader"
```

---

### Task 3: Tool Registry

**Files:**
- Create: `core/tool_registry.py`
- Test: `tests/test_tool_registry.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_tool_registry.py
from core.tool_registry import tool, collect_tools, tools_to_schema


def test_tool_decorator():
    @tool(description="Add two numbers")
    def add(a: int, b: int) -> int:
        return a + b

    assert add._tool_meta["description"] == "Add two numbers"
    assert add(1, 2) == 3  # still callable


def test_collect_tools_from_module():
    import types
    mod = types.ModuleType("fake")

    @tool(description="tool A")
    def func_a() -> str:
        return "a"

    @tool(description="tool B")
    def func_b(x: str) -> str:
        return x

    mod.func_a = func_a
    mod.func_b = func_b
    mod.not_a_tool = lambda: None

    tools = collect_tools(mod)
    assert len(tools) == 2
    names = {t._tool_meta["name"] for t in tools}
    assert names == {"func_a", "func_b"}


def test_tools_to_schema():
    @tool(description="Get weather for a city")
    def get_weather(city: str) -> str:
        return f"sunny in {city}"

    schema = tools_to_schema([get_weather])
    assert len(schema) == 1
    s = schema[0]
    assert s["name"] == "get_weather"
    assert s["description"] == "Get weather for a city"
    assert "city" in s["input_schema"]["properties"]
    assert s["input_schema"]["properties"]["city"]["type"] == "string"
    assert s["input_schema"]["required"] == ["city"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tool_registry.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement tool_registry.py**

```python
# core/tool_registry.py
import inspect
from typing import Any, Callable, get_type_hints

TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def tool(description: str):
    def decorator(func: Callable) -> Callable:
        func._tool_meta = {
            "name": func.__name__,
            "description": description,
        }
        return func
    return decorator


def collect_tools(module) -> list[Callable]:
    tools = []
    for name in dir(module):
        obj = getattr(module, name)
        if callable(obj) and hasattr(obj, "_tool_meta"):
            tools.append(obj)
    return tools


def tools_to_schema(tools: list[Callable]) -> list[dict]:
    schemas = []
    for func in tools:
        meta = func._tool_meta
        hints = get_type_hints(func)
        sig = inspect.signature(func)

        properties = {}
        required = []
        for param_name, param in sig.parameters.items():
            if param_name == "return":
                continue
            param_type = hints.get(param_name, str)
            properties[param_name] = {
                "type": TYPE_MAP.get(param_type, "string"),
            }
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        schemas.append({
            "name": meta["name"],
            "description": meta["description"],
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        })
    return schemas
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tool_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/tool_registry.py tests/test_tool_registry.py
git commit -m "feat: add @tool decorator and schema generation"
```

---

### Task 4: Sandbox

**Files:**
- Create: `core/sandbox.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_sandbox.py
import pytest
from core.sandbox import Sandbox


def test_allow_path_in_allowed_dir(tmp_path):
    sb = Sandbox({"allowed_dirs": [str(tmp_path)], "writable": True})
    sb.check_path(str(tmp_path / "file.txt"), write=False)  # should not raise


def test_deny_path_outside_allowed_dir(tmp_path):
    sb = Sandbox({"allowed_dirs": [str(tmp_path / "safe")]})
    with pytest.raises(PermissionError, match="不在允許目錄"):
        sb.check_path("/etc/passwd", write=False)


def test_deny_pattern(tmp_path):
    sb = Sandbox({
        "allowed_dirs": [str(tmp_path)],
        "denied_patterns": ["*.env", "*.pem"],
    })
    with pytest.raises(PermissionError, match="黑名單"):
        sb.check_path(str(tmp_path / ".env"), write=False)


def test_readonly_sandbox(tmp_path):
    sb = Sandbox({
        "allowed_dirs": [str(tmp_path)],
        "writable": False,
    })
    sb.check_path(str(tmp_path / "file.txt"), write=False)  # read OK
    with pytest.raises(PermissionError, match="唯讀"):
        sb.check_path(str(tmp_path / "file.txt"), write=True)


def test_command_whitelist():
    sb = Sandbox({
        "allowed_dirs": [],
        "allowed_commands": ["git diff", "git log"],
    })
    sb.check_command("git diff HEAD")  # should not raise
    with pytest.raises(PermissionError, match="禁止執行"):
        sb.check_command("rm -rf /")


def test_empty_sandbox_allows_nothing():
    sb = Sandbox({"allowed_dirs": []})
    with pytest.raises(PermissionError):
        sb.check_path("/any/path", write=False)
    with pytest.raises(PermissionError):
        sb.check_command("any command")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sandbox.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement sandbox.py**

```python
# core/sandbox.py
from pathlib import Path
from fnmatch import fnmatch


class Sandbox:
    def __init__(self, config: dict):
        self.allowed_dirs = [Path(d).resolve() for d in config.get("allowed_dirs", [])]
        self.denied_patterns = config.get("denied_patterns", [])
        self.writable = config.get("writable", True)
        self.allowed_commands = config.get("allowed_commands", [])

    def check_path(self, path: str, write: bool = False) -> None:
        resolved = Path(path).resolve()

        if not any(self._is_under(resolved, d) for d in self.allowed_dirs):
            raise PermissionError(f"禁止存取: {path} 不在允許目錄內")

        for pattern in self.denied_patterns:
            if fnmatch(resolved.name, pattern):
                raise PermissionError(f"禁止存取: {path} 符合黑名單 {pattern}")

        if write and not self.writable:
            raise PermissionError(f"此 agent 為唯讀，不能寫入 {path}")

    def check_command(self, command: str) -> None:
        if not self.allowed_commands:
            raise PermissionError(f"禁止執行: {command}")
        if not any(command.startswith(allowed) for allowed in self.allowed_commands):
            raise PermissionError(f"禁止執行: {command}")

    def _is_under(self, path: Path, directory: Path) -> bool:
        try:
            path.relative_to(directory)
            return True
        except ValueError:
            return False
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_sandbox.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/sandbox.py tests/test_sandbox.py
git commit -m "feat: add application-level sandbox for path and command restrictions"
```

---

### Task 5: LLM Wrapper (Claude API + Agentic Loop)

**Files:**
- Create: `core/llm.py`
- Test: `tests/test_llm.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_llm.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.llm import LLMClient


@pytest.mark.asyncio
async def test_simple_text_response():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.stop_reason = "end_turn"
    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = "台北今天 25°C"
    mock_response.content = [mock_text_block]
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    llm = LLMClient(client=mock_client, model="claude-sonnet-4-20250514")
    result = await llm.run(
        system_prompt="你是助手",
        messages=[{"role": "user", "content": "天氣如何"}],
        tools_schema=[],
        tool_executor=None,
    )
    assert result == "台北今天 25°C"


@pytest.mark.asyncio
async def test_tool_call_loop():
    mock_client = MagicMock()

    # First response: tool call
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "tool_1"
    tool_block.name = "get_weather"
    tool_block.input = {"city": "Taipei"}

    first_response = MagicMock()
    first_response.stop_reason = "tool_use"
    first_response.content = [tool_block]

    # Second response: final text
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "台北晴天 25°C"

    second_response = MagicMock()
    second_response.stop_reason = "end_turn"
    second_response.content = [text_block]

    mock_client.messages.create = AsyncMock(
        side_effect=[first_response, second_response]
    )

    async def executor(name, args):
        assert name == "get_weather"
        return "Taipei: 25°C, sunny"

    llm = LLMClient(client=mock_client, model="claude-sonnet-4-20250514")
    result = await llm.run(
        system_prompt="你是助手",
        messages=[{"role": "user", "content": "台北天氣"}],
        tools_schema=[{"name": "get_weather", "description": "get weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}],
        tool_executor=executor,
    )
    assert result == "台北晴天 25°C"
    assert mock_client.messages.create.call_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pip install pytest-asyncio && python -m pytest tests/test_llm.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement llm.py**

```python
# core/llm.py
from typing import Any, Callable, Optional


class LLMClient:
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    async def run(
        self,
        system_prompt: str,
        messages: list[dict],
        tools_schema: list[dict],
        tool_executor: Optional[Callable],
        max_iterations: int = 20,
    ) -> str:
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
                # Extract text from response
                for block in response.content:
                    if block.type == "text":
                        return block.text
                return ""

        return "Error: max iterations reached"
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_llm.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/llm.py tests/test_llm.py
git commit -m "feat: add Claude API wrapper with agentic tool loop"
```

---

### Task 6: Hub Registry

**Files:**
- Create: `hub/registry.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_registry.py
import time
from core.models import AgentInfo
from hub.registry import AgentRegistry


def test_register_and_get():
    reg = AgentRegistry(heartbeat_timeout=30)
    info = AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣"],
    )
    reg.register(info)
    assert reg.get("weather") is not None
    assert reg.get("weather").url == "http://localhost:8001"


def test_list_online():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register(AgentInfo(name="a", description="A", url="http://localhost:8001"))
    reg.register(AgentInfo(name="b", description="B", url="http://localhost:8002"))
    online = reg.list_online()
    assert len(online) == 2


def test_heartbeat_updates_time():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register(AgentInfo(name="a", description="A", url="http://localhost:8001"))
    old_time = reg._last_heartbeat["a"]
    time.sleep(0.01)
    reg.heartbeat("a")
    assert reg._last_heartbeat["a"] > old_time


def test_expired_agent_not_listed():
    reg = AgentRegistry(heartbeat_timeout=0)  # immediately expires
    reg.register(AgentInfo(name="a", description="A", url="http://localhost:8001"))
    time.sleep(0.01)
    online = reg.list_online()
    assert len(online) == 0


def test_unregister():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register(AgentInfo(name="a", description="A", url="http://localhost:8001"))
    reg.unregister("a")
    assert reg.get("a") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_registry.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement registry.py**

```python
# hub/registry.py
import time
from core.models import AgentInfo


class AgentRegistry:
    def __init__(self, heartbeat_timeout: int = 30):
        self._agents: dict[str, AgentInfo] = {}
        self._last_heartbeat: dict[str, float] = {}
        self._heartbeat_timeout = heartbeat_timeout

    def register(self, info: AgentInfo) -> None:
        self._agents[info.name] = info
        self._last_heartbeat[info.name] = time.time()

    def unregister(self, name: str) -> None:
        self._agents.pop(name, None)
        self._last_heartbeat.pop(name, None)

    def heartbeat(self, name: str) -> bool:
        if name not in self._agents:
            return False
        self._last_heartbeat[name] = time.time()
        return True

    def get(self, name: str) -> AgentInfo | None:
        if name not in self._agents:
            return None
        if not self._is_alive(name):
            return None
        return self._agents[name]

    def list_online(self) -> list[AgentInfo]:
        return [info for name, info in self._agents.items() if self._is_alive(name)]

    def _is_alive(self, name: str) -> bool:
        last = self._last_heartbeat.get(name, 0)
        return (time.time() - last) < self._heartbeat_timeout
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hub/registry.py tests/test_registry.py
git commit -m "feat: add agent registry with heartbeat tracking"
```

---

### Task 7: Task Manager

**Files:**
- Create: `hub/task_manager.py`
- Test: `tests/test_task_manager.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_task_manager.py
from hub.task_manager import TaskManager, ManagedTask


def test_create_task():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="台北天氣")
    assert task.agent_name == "weather"
    assert task.chat_id == 123
    assert task.status == "working"
    assert len(task.conversation_history) == 1
    assert task.conversation_history[0]["content"] == "台北天氣"


def test_get_task():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    found = tm.get_task(task.task_id)
    assert found is not None
    assert found.task_id == task.task_id


def test_get_active_task_for_chat():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    active = tm.get_active_task_for_chat(123)
    assert active is not None
    assert active.task_id == task.task_id


def test_append_user_response():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    task.status = "waiting_input"
    tm.append_user_response(task.task_id, "台北")
    assert len(task.conversation_history) == 2
    assert task.conversation_history[1]["content"] == "台北"
    assert task.status == "working"


def test_complete_task():
    tm = TaskManager()
    task = tm.create_task(agent_name="weather", chat_id=123, content="天氣")
    tm.complete_task(task.task_id)
    assert task.status == "done"
    assert tm.get_active_task_for_chat(123) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_task_manager.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement task_manager.py**

```python
# hub/task_manager.py
import uuid
from dataclasses import dataclass, field


@dataclass
class ManagedTask:
    task_id: str
    agent_name: str
    chat_id: int
    status: str  # working, waiting_input, waiting_approval, done
    conversation_history: list[dict] = field(default_factory=list)


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, ManagedTask] = {}

    def create_task(self, agent_name: str, chat_id: int, content: str) -> ManagedTask:
        task_id = str(uuid.uuid4())
        task = ManagedTask(
            task_id=task_id,
            agent_name=agent_name,
            chat_id=chat_id,
            status="working",
            conversation_history=[{"role": "user", "content": content}],
        )
        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> ManagedTask | None:
        return self._tasks.get(task_id)

    def get_active_task_for_chat(self, chat_id: int) -> ManagedTask | None:
        for task in self._tasks.values():
            if task.chat_id == chat_id and task.status not in ("done",):
                return task
        return None

    def append_user_response(self, task_id: str, content: str) -> None:
        task = self._tasks[task_id]
        task.conversation_history.append({"role": "user", "content": content})
        task.status = "working"

    def complete_task(self, task_id: str) -> None:
        self._tasks[task_id].status = "done"
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_task_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hub/task_manager.py tests/test_task_manager.py
git commit -m "feat: add task manager for multi-turn conversation tracking"
```

---

### Task 8: Router (Keyword + Claude Fallback)

**Files:**
- Create: `hub/router.py`
- Test: `tests/test_router.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_router.py
import pytest
from unittest.mock import AsyncMock
from core.models import AgentInfo
from hub.registry import AgentRegistry
from hub.router import Router


def make_registry_with_agents():
    reg = AgentRegistry(heartbeat_timeout=30)
    reg.register(AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣|weather|氣溫"],
    ))
    reg.register(AgentInfo(
        name="code-review",
        description="Code review",
        url="http://localhost:8002",
        route_patterns=["review|PR|code review"],
    ))
    return reg


def test_keyword_match():
    reg = make_registry_with_agents()
    router = Router(registry=reg, llm_fallback=None)
    agent = router.match_by_keyword("台北天氣如何")
    assert agent is not None
    assert agent.name == "weather"


def test_keyword_match_second_agent():
    reg = make_registry_with_agents()
    router = Router(registry=reg, llm_fallback=None)
    agent = router.match_by_keyword("幫我 review 這個 PR")
    assert agent is not None
    assert agent.name == "code-review"


def test_keyword_no_match():
    reg = make_registry_with_agents()
    router = Router(registry=reg, llm_fallback=None)
    agent = router.match_by_keyword("幫我訂機票")
    assert agent is None


@pytest.mark.asyncio
async def test_route_with_keyword():
    reg = make_registry_with_agents()
    router = Router(registry=reg, llm_fallback=None)
    agent = await router.route("今天天氣好嗎")
    assert agent.name == "weather"


@pytest.mark.asyncio
async def test_route_falls_back_to_llm():
    reg = make_registry_with_agents()

    async def mock_fallback(message, agents):
        return "weather"

    router = Router(registry=reg, llm_fallback=mock_fallback)
    agent = await router.route("會不會下雨啊")
    assert agent is not None
    assert agent.name == "weather"


@pytest.mark.asyncio
async def test_route_returns_none_when_no_match():
    reg = make_registry_with_agents()

    async def mock_fallback(message, agents):
        return None

    router = Router(registry=reg, llm_fallback=mock_fallback)
    agent = await router.route("幫我訂機票")
    assert agent is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_router.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement router.py**

```python
# hub/router.py
import re
from typing import Callable, Optional
from core.models import AgentInfo
from hub.registry import AgentRegistry


class Router:
    def __init__(
        self,
        registry: AgentRegistry,
        llm_fallback: Optional[Callable] = None,
    ):
        self.registry = registry
        self.llm_fallback = llm_fallback

    def match_by_keyword(self, message: str) -> AgentInfo | None:
        for agent in self.registry.list_online():
            for pattern in agent.route_patterns:
                if re.search(pattern, message, re.IGNORECASE):
                    return agent
        return None

    async def route(self, message: str) -> AgentInfo | None:
        # Try keyword match first
        agent = self.match_by_keyword(message)
        if agent is not None:
            return agent

        # Fall back to LLM
        if self.llm_fallback is not None:
            online = self.registry.list_online()
            if not online:
                return None
            agent_name = await self.llm_fallback(message, online)
            if agent_name:
                return self.registry.get(agent_name)

        return None
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_router.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hub/router.py tests/test_router.py
git commit -m "feat: add router with keyword matching and LLM fallback"
```

---

### Task 9: BaseAgent

**Files:**
- Create: `core/base_agent.py`
- Test: `tests/test_base_agent.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_base_agent.py
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from core.base_agent import BaseAgent
from core.models import TaskRequest, TaskStatus


class FakeAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest):
        from core.models import AgentResult
        return AgentResult(status=TaskStatus.DONE, message=f"echo: {task.content}")


@pytest.fixture
def agent(tmp_path):
    agent_yaml = tmp_path / "agent.yaml"
    agent_yaml.write_text(
        "name: test-agent\n"
        "description: test\n"
        "route_patterns: ['test']\n"
        "sandbox:\n"
        "  allowed_dirs: []\n"
        "  writable: false\n"
    )
    return FakeAgent(agent_dir=str(tmp_path), hub_url="http://localhost:9000", port=0)


def test_agent_loads_config(agent):
    assert agent.name == "test-agent"
    assert agent.config["description"] == "test"


@pytest.mark.asyncio
async def test_agent_task_endpoint(aiohttp_client, agent):
    app = agent.create_app()
    client = await aiohttp_client(app)

    task = TaskRequest(task_id="t1", content="hello")
    resp = await client.post("/task", json=task.to_dict())
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "done"
    assert data["message"] == "echo: hello"


@pytest.mark.asyncio
async def test_agent_health_endpoint(aiohttp_client, agent):
    app = agent.create_app()
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "test-agent"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pip install pytest-aiohttp && python -m pytest tests/test_base_agent.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement base_agent.py**

```python
# core/base_agent.py
import asyncio
from abc import ABC, abstractmethod
from aiohttp import web, ClientSession
from core.config import load_agent_config
from core.models import AgentInfo, AgentResult, TaskRequest, TaskStatus
from core.sandbox import Sandbox


class BaseAgent(ABC):
    def __init__(self, agent_dir: str, hub_url: str, port: int = 0):
        self.config = load_agent_config(agent_dir)
        self.name = self.config["name"]
        self.hub_url = hub_url
        self.port = port
        sandbox_config = self.config.get("sandbox", {"allowed_dirs": []})
        self.sandbox = Sandbox(sandbox_config)

    @abstractmethod
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        pass

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/task", self._handle_task_http)
        app.router.add_get("/health", self._handle_health)
        return app

    async def _handle_task_http(self, request: web.Request) -> web.Response:
        data = await request.json()
        task = TaskRequest.from_dict(data)
        result = await self.handle_task(task)
        return web.json_response(result.to_dict())

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"name": self.name, "status": "ok"})

    async def register(self, actual_port: int) -> None:
        info = AgentInfo(
            name=self.name,
            description=self.config.get("description", ""),
            url=f"http://localhost:{actual_port}",
            route_patterns=self.config.get("route_patterns", []),
            capabilities=self.config.get("capabilities", []),
        )
        async with ClientSession() as session:
            await session.post(f"{self.hub_url}/register", json=info.to_dict())

    async def _heartbeat_loop(self, interval: int = 10) -> None:
        async with ClientSession() as session:
            while True:
                try:
                    await session.post(
                        f"{self.hub_url}/heartbeat",
                        json={"name": self.name},
                    )
                except Exception:
                    pass
                await asyncio.sleep(interval)

    async def run(self) -> None:
        app = self.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()

        actual_port = site._server.sockets[0].getsockname()[1]
        print(f"Agent '{self.name}' running on port {actual_port}")

        await self.register(actual_port)
        await self._heartbeat_loop()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_base_agent.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/base_agent.py tests/test_base_agent.py
git commit -m "feat: add BaseAgent with HTTP server, registration, and heartbeat"
```

---

### Task 10: Hub Server

**Files:**
- Create: `hub/server.py`
- Test: `tests/test_hub_server.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_hub_server.py
import pytest
from hub.server import create_hub_app
from core.models import AgentInfo


@pytest.mark.asyncio
async def test_register_agent(aiohttp_client):
    app = create_hub_app()
    client = await aiohttp_client(app)

    info = AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣"],
    )
    resp = await client.post("/register", json=info.to_dict())
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "registered"


@pytest.mark.asyncio
async def test_heartbeat(aiohttp_client):
    app = create_hub_app()
    client = await aiohttp_client(app)

    # Register first
    info = AgentInfo(name="weather", description="查天氣", url="http://localhost:8001")
    await client.post("/register", json=info.to_dict())

    # Heartbeat
    resp = await client.post("/heartbeat", json={"name": "weather"})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_heartbeat_unknown_agent(aiohttp_client):
    app = create_hub_app()
    client = await aiohttp_client(app)

    resp = await client.post("/heartbeat", json={"name": "nonexistent"})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_list_agents(aiohttp_client):
    app = create_hub_app()
    client = await aiohttp_client(app)

    info = AgentInfo(name="weather", description="查天氣", url="http://localhost:8001")
    await client.post("/register", json=info.to_dict())

    resp = await client.get("/agents")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["agents"]) == 1
    assert data["agents"][0]["name"] == "weather"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hub_server.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement server.py**

```python
# hub/server.py
from aiohttp import web
from core.models import AgentInfo
from hub.registry import AgentRegistry
from hub.task_manager import TaskManager
from hub.router import Router


def create_hub_app(
    heartbeat_timeout: int = 30,
    llm_fallback=None,
) -> web.Application:
    app = web.Application()
    registry = AgentRegistry(heartbeat_timeout=heartbeat_timeout)
    task_manager = TaskManager()
    router = Router(registry=registry, llm_fallback=llm_fallback)

    app["registry"] = registry
    app["task_manager"] = task_manager
    app["router"] = router

    app.router.add_post("/register", handle_register)
    app.router.add_post("/heartbeat", handle_heartbeat)
    app.router.add_get("/agents", handle_list_agents)

    return app


async def handle_register(request: web.Request) -> web.Response:
    data = await request.json()
    info = AgentInfo.from_dict(data)
    request.app["registry"].register(info)
    return web.json_response({"status": "registered", "name": info.name})


async def handle_heartbeat(request: web.Request) -> web.Response:
    data = await request.json()
    name = data["name"]
    success = request.app["registry"].heartbeat(name)
    if not success:
        return web.json_response({"error": "agent not found"}, status=404)
    return web.json_response({"status": "ok"})


async def handle_list_agents(request: web.Request) -> web.Response:
    agents = request.app["registry"].list_online()
    return web.json_response({"agents": [a.to_dict() for a in agents]})
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_hub_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hub/server.py tests/test_hub_server.py
git commit -m "feat: add Hub HTTP server with register, heartbeat, and list endpoints"
```

---

### Task 11: Hub CLI (Development Test Interface)

**Files:**
- Create: `hub/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_cli.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from aiohttp import ClientSession
from hub.cli import dispatch_message


@pytest.mark.asyncio
async def test_dispatch_to_agent():
    mock_router = AsyncMock()
    mock_router.route = AsyncMock(return_value=MagicMock(
        name="weather",
        url="http://localhost:8001",
    ))

    mock_response = {"status": "done", "message": "台北 25°C 晴天"}

    with patch("hub.cli.send_task_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = mock_response
        result = await dispatch_message(
            message="台北天氣",
            router=mock_router,
            task_manager=MagicMock(),
        )
    assert result["message"] == "台北 25°C 晴天"


@pytest.mark.asyncio
async def test_dispatch_no_agent_found():
    mock_router = AsyncMock()
    mock_router.route = AsyncMock(return_value=None)

    result = await dispatch_message(
        message="幫我訂機票",
        router=mock_router,
        task_manager=MagicMock(),
    )
    assert result["status"] == "error"
    assert "無法處理" in result["message"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement cli.py**

```python
# hub/cli.py
import asyncio
import sys
from aiohttp import ClientSession
from core.models import TaskRequest
from hub.router import Router
from hub.task_manager import TaskManager
import uuid


async def send_task_to_agent(agent_url: str, task: TaskRequest) -> dict:
    async with ClientSession() as session:
        async with session.post(f"{agent_url}/task", json=task.to_dict()) as resp:
            return await resp.json()


async def dispatch_message(
    message: str,
    router: Router,
    task_manager: TaskManager,
) -> dict:
    agent = await router.route(message)
    if agent is None:
        return {"status": "error", "message": "無法處理此訊息，沒有可用的 agent"}

    task = task_manager.create_task(
        agent_name=agent.name,
        chat_id=0,  # CLI mode, no chat_id
        content=message,
    )
    task_request = TaskRequest(
        task_id=task.task_id,
        content=message,
        conversation_history=task.conversation_history,
    )
    result = await send_task_to_agent(agent.url, task_request)

    if result.get("status") == "done":
        task_manager.complete_task(task.task_id)
    elif result.get("status") in ("need_input", "need_approval"):
        task.status = f"waiting_{result['status'].split('_')[1]}"

    return result


async def cli_loop(hub_url: str = "http://localhost:9000") -> None:
    from hub.server import create_hub_app

    # Get router and task_manager from a running hub
    async with ClientSession() as session:
        print("Agent Platform CLI (輸入 'quit' 離開)")
        print("-" * 40)

        while True:
            try:
                message = input("\n你: ")
            except (EOFError, KeyboardInterrupt):
                print("\n再見！")
                break

            if message.strip().lower() in ("quit", "exit"):
                print("再見！")
                break

            # Send to hub for routing
            async with session.post(
                f"{hub_url}/dispatch",
                json={"message": message, "chat_id": 0},
            ) as resp:
                result = await resp.json()

            status = result.get("status")
            print(f"\nAgent: {result.get('message', '')}")

            if status in ("need_input", "need_approval"):
                options = result.get("options")
                if options:
                    for i, opt in enumerate(options, 1):
                        print(f"  {i}. {opt}")


def main():
    asyncio.run(cli_loop())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hub/cli.py tests/test_cli.py
git commit -m "feat: add CLI test interface for dispatching messages"
```

---

### Task 12: Hub Dispatch Endpoint

**Files:**
- Modify: `hub/server.py`
- Test: `tests/test_dispatch.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_dispatch.py
import pytest
from aiohttp import web
from unittest.mock import AsyncMock, patch
from hub.server import create_hub_app
from core.models import AgentInfo


@pytest.mark.asyncio
async def test_dispatch_routes_to_agent(aiohttp_client):
    app = create_hub_app()
    client = await aiohttp_client(app)

    # Register a mock agent
    info = AgentInfo(
        name="weather",
        description="查天氣",
        url="http://localhost:8001",
        route_patterns=["天氣"],
    )
    await client.post("/register", json=info.to_dict())

    # Mock the HTTP call to the agent
    mock_result = {"status": "done", "message": "台北 25°C"}
    with patch("hub.server.send_task_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = mock_result
        resp = await client.post("/dispatch", json={"message": "台北天氣", "chat_id": 0})

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "done"
    assert data["message"] == "台北 25°C"


@pytest.mark.asyncio
async def test_dispatch_no_agent(aiohttp_client):
    app = create_hub_app()
    client = await aiohttp_client(app)

    resp = await client.post("/dispatch", json={"message": "訂機票", "chat_id": 0})
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dispatch.py -v`
Expected: FAIL — no `/dispatch` endpoint

- [ ] **Step 3: Add dispatch endpoint to server.py**

Add to `hub/server.py`:

```python
# Add import at top
from hub.cli import send_task_to_agent

# Add in create_hub_app, after existing routes:
    app.router.add_post("/dispatch", handle_dispatch)

# Add handler function:
async def handle_dispatch(request: web.Request) -> web.Response:
    data = await request.json()
    message = data["message"]
    chat_id = data.get("chat_id", 0)

    router = request.app["router"]
    task_manager = request.app["task_manager"]

    # Check for active task (multi-turn continuation)
    active_task = task_manager.get_active_task_for_chat(chat_id)
    if active_task and active_task.status in ("waiting_input", "waiting_approval"):
        task_manager.append_user_response(active_task.task_id, message)
        task_request = TaskRequest(
            task_id=active_task.task_id,
            content=message,
            conversation_history=active_task.conversation_history,
        )
        agent_info = request.app["registry"].get(active_task.agent_name)
        if agent_info is None:
            return web.json_response({"status": "error", "message": "Agent 已離線"})
        result = await send_task_to_agent(agent_info.url, task_request)
    else:
        # New task — route it
        agent = await router.route(message)
        if agent is None:
            return web.json_response({
                "status": "error",
                "message": "無法處理此訊息，沒有可用的 agent",
            })

        task = task_manager.create_task(
            agent_name=agent.name, chat_id=chat_id, content=message,
        )
        task_request = TaskRequest(
            task_id=task.task_id,
            content=message,
            conversation_history=task.conversation_history,
        )
        result = await send_task_to_agent(agent.url, task_request)
        active_task = task

    # Update task status based on result
    status = result.get("status")
    if status == "done":
        task_manager.complete_task(active_task.task_id)
    elif status == "need_input":
        active_task.status = "waiting_input"
    elif status == "need_approval":
        active_task.status = "waiting_approval"

    return web.json_response(result)
```

Also add the `TaskRequest` import at the top of `hub/server.py`:

```python
from core.models import AgentInfo, TaskRequest
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_dispatch.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hub/server.py tests/test_dispatch.py
git commit -m "feat: add /dispatch endpoint with multi-turn task support"
```

---

### Task 13: Weather Agent

**Files:**
- Create: `agents/weather/agent.yaml`
- Create: `agents/weather/tools.py`
- Create: `agents/weather/prompts.py`
- Create: `agents/weather/__init__.py`
- Create: `agents/weather/__main__.py`
- Test: `tests/test_weather_agent.py`

- [ ] **Step 1: Create agent.yaml**

```yaml
# agents/weather/agent.yaml
name: weather-agent
description: "查詢天氣資訊：目前天氣、未來預報"
route_patterns:
  - "天氣|weather|氣溫|溫度|下雨"
sandbox:
  allowed_dirs: []
  writable: false
  allowed_commands: []
  network:
    allow:
      - "wttr.in"
```

- [ ] **Step 2: Create prompts.py**

```python
# agents/weather/prompts.py
SYSTEM_PROMPT = """你是一個天氣查詢助手。

你的職責：
- 收到城市名稱後，使用 get_weather tool 查詢天氣
- 以簡潔的中文回報天氣資訊
- 若使用者未指定城市，主動詢問想查詢哪個城市的天氣

回報格式簡潔明瞭，包含溫度、天氣狀況即可。"""
```

- [ ] **Step 3: Write failing test for weather tool**

```python
# tests/test_weather_agent.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from agents.weather.tools import get_weather


@pytest.mark.asyncio
async def test_get_weather_returns_info():
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="Weather: Taipei\n25°C\nSunny")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=mock_response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("agents.weather.tools.ClientSession", return_value=mock_session):
        result = await get_weather("Taipei")
    assert "Taipei" in result or "25" in result


@pytest.mark.asyncio
async def test_get_weather_handles_error():
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.text = AsyncMock(return_value="error")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=mock_response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("agents.weather.tools.ClientSession", return_value=mock_session):
        result = await get_weather("Taipei")
    assert "錯誤" in result or "error" in result.lower()
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python -m pytest tests/test_weather_agent.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 5: Implement tools.py**

```python
# agents/weather/tools.py
from aiohttp import ClientSession
from core.tool_registry import tool


@tool(description="查詢指定城市的天氣資訊")
async def get_weather(city: str) -> str:
    url = f"https://wttr.in/{city}?format=3&lang=zh-tw"
    try:
        async with ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.text()
                return f"查詢天氣時發生錯誤 (HTTP {resp.status})"
    except Exception as e:
        return f"查詢天氣時發生錯誤: {e}"
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_weather_agent.py -v`
Expected: PASS

- [ ] **Step 7: Create __init__.py and __main__.py**

`agents/weather/__init__.py`:
```python
```

`agents/weather/__main__.py`:
```python
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus
from core.llm import LLMClient
from core.tool_registry import collect_tools, tools_to_schema
from agents.weather import tools as weather_tools
from agents.weather.prompts import SYSTEM_PROMPT
import anthropic


class WeatherAgent(BaseAgent):
    def __init__(self, hub_url: str, port: int = 0):
        agent_dir = os.path.dirname(os.path.abspath(__file__))
        super().__init__(agent_dir=agent_dir, hub_url=hub_url, port=port)

        client = anthropic.AsyncAnthropic()
        self.llm = LLMClient(client=client, model="claude-sonnet-4-20250514")
        self.tools = collect_tools(weather_tools)
        self.tools_schema = tools_to_schema(self.tools)
        self._tool_map = {t._tool_meta["name"]: t for t in self.tools}

    async def handle_task(self, task: TaskRequest) -> AgentResult:
        async def execute_tool(name: str, args: dict) -> str:
            func = self._tool_map.get(name)
            if func is None:
                return f"Unknown tool: {name}"
            return await func(**args)

        try:
            result = await self.llm.run(
                system_prompt=SYSTEM_PROMPT,
                messages=task.conversation_history or [{"role": "user", "content": task.content}],
                tools_schema=self.tools_schema,
                tool_executor=execute_tool,
            )
            return AgentResult(status=TaskStatus.DONE, message=result)
        except Exception as e:
            return AgentResult(status=TaskStatus.ERROR, message=f"處理失敗: {e}")


async def main():
    hub_url = os.environ.get("HUB_URL", "http://localhost:9000")
    port = int(os.environ.get("AGENT_PORT", "0"))
    agent = WeatherAgent(hub_url=hub_url, port=port)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 8: Commit**

```bash
git add agents/weather/
git commit -m "feat: add weather agent with wttr.in integration"
```

---

### Task 14: Integration Test (CLI → Hub → Weather Agent)

**Files:**
- Create: `tests/test_integration.py`
- Create: `run_hub.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_integration.py
import pytest
import asyncio
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer
from unittest.mock import AsyncMock, patch
from hub.server import create_hub_app
from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus


class EchoAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        return AgentResult(status=TaskStatus.DONE, message=f"echo: {task.content}")


@pytest.mark.asyncio
async def test_full_flow_hub_to_agent(tmp_path):
    """Test: Hub receives message → routes to agent → returns result"""

    # 1. Create agent config
    agent_dir = tmp_path / "echo"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "name: echo-agent\n"
        "description: Echo messages back\n"
        "route_patterns:\n"
        "  - 'echo|測試'\n"
        "sandbox:\n"
        "  allowed_dirs: []\n"
    )

    # 2. Start hub
    hub_app = create_hub_app()
    hub_server = TestServer(hub_app)
    await hub_server.start_server()
    hub_url = f"http://localhost:{hub_server.port}"

    # 3. Start echo agent
    agent = EchoAgent(agent_dir=str(agent_dir), hub_url=hub_url, port=0)
    agent_app = agent.create_app()
    agent_server = TestServer(agent_app)
    await agent_server.start_server()
    agent_url = f"http://localhost:{agent_server.port}"

    try:
        # 4. Register agent with hub
        async with ClientSession() as session:
            await session.post(f"{hub_url}/register", json={
                "name": "echo-agent",
                "description": "Echo messages back",
                "url": agent_url,
                "route_patterns": ["echo|測試"],
            })

            # 5. Dispatch message through hub
            async with session.post(f"{hub_url}/dispatch", json={
                "message": "echo hello",
                "chat_id": 1,
            }) as resp:
                result = await resp.json()

        assert result["status"] == "done"
        assert result["message"] == "echo: echo hello"
    finally:
        await hub_server.close()
        await agent_server.close()


@pytest.mark.asyncio
async def test_multi_turn_flow(tmp_path):
    """Test: Agent asks for input → user responds → agent completes"""

    class AskAgent(BaseAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._call_count = 0

        async def handle_task(self, task: TaskRequest) -> AgentResult:
            self._call_count += 1
            if self._call_count == 1:
                return AgentResult(
                    status=TaskStatus.NEED_INPUT,
                    message="哪個城市？",
                )
            last_msg = task.conversation_history[-1]["content"]
            return AgentResult(
                status=TaskStatus.DONE,
                message=f"天氣: {last_msg} 25°C",
            )

    agent_dir = tmp_path / "ask"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "name: ask-agent\n"
        "description: Asks then answers\n"
        "route_patterns:\n"
        "  - '天氣'\n"
        "sandbox:\n"
        "  allowed_dirs: []\n"
    )

    hub_app = create_hub_app()
    hub_server = TestServer(hub_app)
    await hub_server.start_server()
    hub_url = f"http://localhost:{hub_server.port}"

    agent = AskAgent(agent_dir=str(agent_dir), hub_url=hub_url, port=0)
    agent_app = agent.create_app()
    agent_server = TestServer(agent_app)
    await agent_server.start_server()
    agent_url = f"http://localhost:{agent_server.port}"

    try:
        async with ClientSession() as session:
            # Register
            await session.post(f"{hub_url}/register", json={
                "name": "ask-agent",
                "description": "Asks then answers",
                "url": agent_url,
                "route_patterns": ["天氣"],
            })

            # First message
            async with session.post(f"{hub_url}/dispatch", json={
                "message": "查天氣", "chat_id": 42,
            }) as resp:
                r1 = await resp.json()
            assert r1["status"] == "need_input"
            assert r1["message"] == "哪個城市？"

            # User responds
            async with session.post(f"{hub_url}/dispatch", json={
                "message": "台北", "chat_id": 42,
            }) as resp:
                r2 = await resp.json()
            assert r2["status"] == "done"
            assert "台北" in r2["message"]
    finally:
        await hub_server.close()
        await agent_server.close()
```

- [ ] **Step 2: Run integration tests**

Run: `python -m pytest tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 3: Create run_hub.py entry point**

```python
# run_hub.py
import asyncio
from aiohttp import web
from hub.server import create_hub_app
from core.config import load_config


def main():
    config = load_config("config.yaml")
    hub_config = config.get("hub", {})
    app = create_hub_app(heartbeat_timeout=hub_config.get("heartbeat_timeout", 30))

    host = hub_config.get("host", "0.0.0.0")
    port = hub_config.get("port", 9000)
    print(f"Hub starting on {host}:{port}")
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py run_hub.py
git commit -m "feat: add integration tests and hub entry point"
```

---

### Task 15: Telegram Bot

**Files:**
- Create: `hub/bot.py`
- Modify: `config.yaml`

- [ ] **Step 1: Add TG bot token to config**

Update `config.yaml`:
```yaml
hub:
  host: "0.0.0.0"
  port: 9000
  heartbeat_timeout: 30

llm:
  model: "claude-sonnet-4-20250514"

telegram:
  token_env: "TELEGRAM_BOT_TOKEN"  # reads from environment variable
```

- [ ] **Step 2: Implement bot.py**

```python
# hub/bot.py
import os
import asyncio
import logging
from aiohttp import ClientSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, token: str, hub_url: str):
        self.token = token
        self.hub_url = hub_url

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Agent Platform 已啟動！直接輸入訊息，我會分配給對應的 agent 處理。"
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message.text
        chat_id = update.effective_chat.id

        async with ClientSession() as session:
            async with session.post(
                f"{self.hub_url}/dispatch",
                json={"message": message, "chat_id": chat_id},
            ) as resp:
                result = await resp.json()

        status = result.get("status")
        text = result.get("message", "")
        options = result.get("options")

        if status == "need_approval":
            text = f"⚠️ {text}"

        if options:
            keyboard = [
                [InlineKeyboardButton(opt, callback_data=opt)]
                for opt in options
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = query.data
        chat_id = update.effective_chat.id

        async with ClientSession() as session:
            async with session.post(
                f"{self.hub_url}/dispatch",
                json={"message": choice, "chat_id": chat_id},
            ) as resp:
                result = await resp.json()

        text = result.get("message", "")
        options = result.get("options")

        if result.get("status") == "need_approval":
            text = f"⚠️ {text}"

        if options:
            keyboard = [
                [InlineKeyboardButton(opt, callback_data=opt)]
                for opt in options
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup)
        else:
            await query.edit_message_text(f"{query.message.text}\n\n✅ 選擇: {choice}")
            await query.message.reply_text(text)

    def run(self):
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        app.add_handler(CallbackQueryHandler(self.handle_callback))

        logger.info("Telegram Bot started")
        app.run_polling()


def main():
    from core.config import load_config

    config = load_config("config.yaml")
    token_env = config.get("telegram", {}).get("token_env", "TELEGRAM_BOT_TOKEN")
    token = os.environ.get(token_env)
    if not token:
        print(f"Error: Set {token_env} environment variable")
        return

    hub_config = config.get("hub", {})
    hub_url = f"http://localhost:{hub_config.get('port', 9000)}"

    bot = TelegramBot(token=token, hub_url=hub_url)
    bot.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Commit**

```bash
git add hub/bot.py config.yaml
git commit -m "feat: add Telegram Bot with inline keyboard support"
```

---

### Task 16: Update core/__init__.py exports and final wiring

**Files:**
- Modify: `core/__init__.py`

- [ ] **Step 1: Update core exports**

```python
# core/__init__.py
from core.models import AgentResult, TaskRequest, AgentInfo, TaskStatus
from core.config import load_config, load_agent_config
from core.tool_registry import tool, collect_tools, tools_to_schema
from core.sandbox import Sandbox
from core.llm import LLMClient
from core.base_agent import BaseAgent
```

- [ ] **Step 2: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add core/__init__.py
git commit -m "feat: update core exports for complete module access"
```

---

## Quick Start (After Implementation)

```bash
# Terminal 1: Start Hub
python run_hub.py

# Terminal 2: Start Weather Agent
python -m agents.weather

# Terminal 3: Test via CLI or TG Bot
# CLI:
python -c "
import asyncio
from hub.cli import cli_loop
asyncio.run(cli_loop())
"

# Or TG Bot:
TELEGRAM_BOT_TOKEN=xxx python -m hub.bot
```
