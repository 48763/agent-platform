# Agent 開發指南

本文件說明如何開發新的 Agent 並接入 Hub 平台。開發新 Agent 只需寫業務邏輯與設定檔，共用功能由 `core/` 模組提供。

## 概念

每個 Agent 是獨立的 Docker container，啟動後自動向 Hub 註冊。Agent 只需做一件事：**接收 `TaskRequest`，回傳 `AgentResult`**。

```python
from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus

class MyAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        return AgentResult(status=TaskStatus.DONE, message="完成")
```

## 共用模組（core/）

### BaseAgent

Agent 基底類別，繼承即可使用。自動處理：
- HTTP server（`/task` 和 `/health` 端點）
- Hub 註冊（name、description、route_patterns、priority）
- 每 10 秒 heartbeat（Hub 重啟時自動重新註冊）
- 載入 `agent.yaml` 設定
- 初始化 Sandbox 安全限制

### TaskRequest

Hub 發送給 Agent 的任務請求。

```python
@dataclass
class TaskRequest:
    task_id: str                          # 唯一 task ID
    content: str                          # 使用者訊息內容
    conversation_history: list[dict]      # 完整對話歷史
    # [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
```

### AgentResult

Agent 回傳給 Hub 的結果。

```python
@dataclass
class AgentResult:
    status: TaskStatus    # DONE / NEED_INPUT / NEED_APPROVAL / ERROR
    message: str          # 回覆內容
    options: list[str]    # 可選，選項按鈕
    action: str           # 可選，需授權的操作描述
```

| status | 意思 | Hub 行為 |
|--------|------|---------|
| `DONE` | 任務完成 | task 標為 done |
| `NEED_INPUT` | 需要更多資訊 | 等使用者回覆後再送來 |
| `NEED_APPROVAL` | 需要使用者授權 | 等使用者回覆後再送來 |
| `ERROR` | 執行失敗 | task 標為 done |

### Sandbox

路徑和指令的安全限制，由 `agent.yaml` 的 `sandbox` 設定控制。

```python
self.sandbox.check_path("/some/path", write=True)   # 檢查是否允許寫入
self.sandbox.check_command("git diff")               # 檢查是否允許執行
```

### ToolRegistry + LLMClient

用於需要 Claude API 的 Agent。

```python
from core.tool_registry import tool, collect_tools, tools_to_schema
from core.llm import LLMClient

@tool(description="查詢天氣")
async def get_weather(city: str) -> str:
    return f"{city} 25°C"

# 自動產生 Claude API tool schema
tools = collect_tools(my_module)
schema = tools_to_schema(tools)

# Agentic loop — 自動執行 tool call 直到最終回答
llm = LLMClient(client=anthropic_client, model="claude-sonnet-4-20250514")
result = await llm.run(system_prompt="...", messages=[...], tools_schema=schema, tool_executor=executor)
```

## 建立新 Agent

### 1. 目錄結構

```
agents/my_agent/
├── agent.yaml        # 設定
├── __init__.py       # 空
├── __main__.py       # 入口
├── README.md         # 這個 agent 的說明文件
└── Dockerfile        # 容器
```

### 2. agent.yaml

```yaml
name: my-agent
description: "這個 agent 做什麼"
priority: 5                     # 數字越小，關鍵字比對越優先
route_patterns:
  - "關鍵字A|關鍵字B|keyword"   # regex，任一匹配即分配到此 agent
sandbox:
  allowed_dirs: []
  writable: false
```

### 3. __main__.py

```python
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus


class MyAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        return AgentResult(status=TaskStatus.DONE, message=f"收到: {task.content}")


async def main():
    hub_url = os.environ.get("HUB_URL", "http://localhost:9000")
    port = int(os.environ.get("AGENT_PORT", "0"))
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    agent = MyAgent(agent_dir=agent_dir, hub_url=hub_url, port=port)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
```

### 4. Dockerfile

```dockerfile
FROM python:3.12-alpine
WORKDIR /app
RUN apk add --no-cache gcc musl-dev
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core/ ./core/
COPY agents/ ./agents/
CMD ["python", "-m", "agents.my_agent"]
```

### 5. 環境變數

建立 `.env/my-agent.env`：

```env
HUB_URL=http://hub:9000
AGENT_HOST=my-agent
AGENT_PORT=8010
```

### 6. docker-compose.yaml

```yaml
  my-agent:
    build:
      context: .
      dockerfile: agents/my_agent/Dockerfile
    env_file:
      - .env/my-agent.env
    depends_on:
      hub:
        condition: service_healthy
    networks:
      - agent-network
```

### 7. 啟動

```bash
docker compose build my-agent
docker compose up my-agent -d
```

Agent 啟動後自動註冊，Dashboard 即可看到。

## 多輪對話

Hub 管理對話狀態，Agent 只需根據 `task.conversation_history` 判斷對話階段。

```python
class MyAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        history = task.conversation_history

        if len(history) == 1:
            return AgentResult(status=TaskStatus.NEED_INPUT, message="你想查哪個城市？")

        city = history[-1]["content"]
        return AgentResult(status=TaskStatus.DONE, message=f"{city} 25°C")
```

## 進階範例

| Agent | 特點 | 路徑 |
|-------|------|------|
| Claude Code | CLI subprocess + JSON stream + 多輪 session | `agents/claude_code/` |
| TG Transfer | 獨立 Telethon client + SQLite + pHash + 搜尋 + Dashboard + 背景 task | `agents/tg_transfer/` |

各 agent 的詳細文件請見其目錄下的 `README.md`。

## 規範

- 語言：Python
- commit 不加 Co-Authored-By
- 使用繁體中文回覆使用者
- priority 數字越小越優先匹配
- Agent 不需管理對話狀態，Hub 帶完整 conversation_history
