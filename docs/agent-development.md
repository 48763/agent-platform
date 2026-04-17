# Agent 開發指南

本文件說明如何基於 `core` 共用模組開發新的 Agent，並接入 Hub 平台。

## 概念

每個 Agent 是一個獨立的 Docker container，啟動後自動向 Hub 註冊。Hub 根據關鍵字和 Gemini 判斷將使用者訊息分配給對應的 Agent 處理。

Agent 只需要做一件事：**接收 `TaskRequest`，回傳 `AgentResult`**。

## 共用模組

`core/` 目錄提供以下共用元件，所有 Agent 都可直接引用：

### BaseAgent

Agent 的基底類別，處理 HTTP server、Hub 註冊、heartbeat 等基礎功能。

```python
from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus

class MyAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        # 你的業務邏輯
        return AgentResult(status=TaskStatus.DONE, message="完成")
```

BaseAgent 自動處理：
- 啟動 HTTP server（`/task` 和 `/health` 端點）
- 向 Hub 註冊（帶上 name、description、route_patterns、priority）
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
    # conversation_history 格式：
    # [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
```

### AgentResult

Agent 回傳給 Hub 的結果。

```python
@dataclass
class AgentResult:
    status: TaskStatus    # DONE / NEED_INPUT / NEED_APPROVAL / ERROR
    message: str          # 回覆內容
    options: list[str]    # 可選，選項按鈕（TG Inline Keyboard）
    action: str           # 可選，需授權的操作描述
```

**四種狀態：**

| status | 意思 | Hub 行為 | TG 呈現 |
|--------|------|---------|---------|
| `DONE` | 任務完成 | task 標為 done | 一般訊息 |
| `NEED_INPUT` | 需要更多資訊 | task 標為 waiting_input，等使用者回覆後再送來 | 一般訊息或按鈕 |
| `NEED_APPROVAL` | 需要使用者授權 | task 標為 waiting_approval，等使用者回覆後再送來 | ⚠️ 醒目提示 + 按鈕 |
| `ERROR` | 執行失敗 | task 標為 done | 錯誤訊息 |

### Sandbox

路徑和指令的安全限制，由 `agent.yaml` 的 `sandbox` 設定控制。

```python
# Agent 內使用
self.sandbox.check_path("/some/path", write=True)   # 檢查是否允許寫入
self.sandbox.check_command("git diff")               # 檢查是否允許執行
```

### ToolRegistry

用 `@tool` 裝飾器定義 Agent 的工具，自動產生 Claude API 的 tool schema。

```python
from core.tool_registry import tool, collect_tools, tools_to_schema

@tool(description="查詢天氣")
async def get_weather(city: str) -> str:
    return f"{city} 25°C"

tools = collect_tools(my_module)           # 從模組收集所有 @tool
schema = tools_to_schema(tools)            # 轉成 Claude API schema
```

### LLMClient

Claude API 封裝，含 agentic loop（自動執行 tool call 直到最終回答）。

```python
from core.llm import LLMClient
import anthropic

client = anthropic.AsyncAnthropic()
llm = LLMClient(client=client, model="claude-sonnet-4-20250514")

result = await llm.run(
    system_prompt="你是助手",
    messages=[{"role": "user", "content": "你好"}],
    tools_schema=schema,
    tool_executor=my_executor,
)
```

## 建立新 Agent

### 1. 建立目錄

```
agents/my_agent/
├── agent.yaml        # 設定
├── __init__.py       # 空
├── __main__.py       # 入口
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
  allowed_dirs:
    - /workspace/my-project     # 允許存取的目錄
  denied_patterns:
    - "*.env"                   # 黑名單
    - "*.pem"
  writable: true                # 是否允許寫入
  allowed_commands:             # 允許的 shell 指令（前綴匹配）
    - "git diff"
    - "git log"
```

### 3. __main__.py

**最簡範例（直接回覆）：**

```python
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus


class MyAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        # task.content = 使用者訊息
        # task.conversation_history = 完整對話歷史
        return AgentResult(
            status=TaskStatus.DONE,
            message=f"收到: {task.content}",
        )


async def main():
    hub_url = os.environ.get("HUB_URL", "http://localhost:9000")
    port = int(os.environ.get("AGENT_PORT", "0"))
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    agent = MyAgent(agent_dir=agent_dir, hub_url=hub_url, port=port)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
```

**追問範例（需要更多資訊）：**

```python
class MyAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        history = task.conversation_history

        # 第一次：追問
        if len(history) == 1:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="你想查哪個城市？",
            )

        # 第二次：有了城市，回覆
        city = history[-1]["content"]
        return AgentResult(
            status=TaskStatus.DONE,
            message=f"{city} 的天氣是 25°C",
        )
```

**帶選項的範例（按鈕）：**

```python
class MyAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        history = task.conversation_history

        if len(history) == 1:
            return AgentResult(
                status=TaskStatus.NEED_INPUT,
                message="選一個環境：",
                options=["staging", "production"],  # TG 會顯示按鈕
            )

        env = history[-1]["content"]
        return AgentResult(
            status=TaskStatus.DONE,
            message=f"已部署到 {env}",
        )
```

**權限確認範例：**

```python
class MyAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        history = task.conversation_history

        if len(history) == 1:
            return AgentResult(
                status=TaskStatus.NEED_APPROVAL,
                message="要執行 rm -rf dist/ 嗎？",
                action="rm -rf dist/",
                options=["允許", "拒絕"],  # TG 顯示 ⚠️ + 按鈕
            )

        if history[-1]["content"] == "允許":
            # 執行操作
            return AgentResult(status=TaskStatus.DONE, message="已執行")
        else:
            return AgentResult(status=TaskStatus.DONE, message="已取消")
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

建立 `.env/example/my-agent.env`（同上，作為範本 tracked in git）。

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

Agent 啟動後會自動向 Hub 註冊，Dashboard 可以看到。

## 多輪對話

Hub 管理對話狀態，Agent 只需要根據 `task.conversation_history` 判斷對話進行到哪裡。

```
使用者: "幫我部署"
  → Agent 收到 history: [{role: "user", content: "幫我部署"}]
  → 回傳 NEED_INPUT "部署到哪個環境？"

使用者: "staging"
  → Agent 收到 history: [
      {role: "user", content: "幫我部署"},
      {role: "assistant", content: "部署到哪個環境？"},
      {role: "user", content: "staging"},
    ]
  → 回傳 DONE "已部署到 staging"
```

Agent 不需要自己管理對話狀態，Hub 會把完整歷史帶過來。

## 掛載目錄

如果 Agent 需要存取本機檔案（例如 code review），在 `docker-compose.yaml` 掛載：

```yaml
  my-agent:
    volumes:
      - ~/projects:/workspace    # 本機目錄 → 容器內路徑
```

搭配 `agent.yaml` 的 sandbox 限制存取範圍。

## 需要 CLI 認證的 Agent

如果 Agent 使用需要登入的 CLI 工具（例如 Claude Code、Gemini CLI），掛載認證目錄：

```yaml
  my-agent:
    volumes:
      - ./data/my-agent/.config:/root/.config    # 認證資料
      - ~/projects:/workspace
```

首次在容器內手動登入：

```bash
docker exec -it agent-my-agent-1 sh
# 執行登入指令，複製 URL 到瀏覽器，貼回 code
```

認證資料保存在 `data/my-agent/`，container 重啟不需重新登入。

## 測試

在本地不透過 Docker 測試：

```bash
source .venv/bin/activate

# 先啟動 Hub
python run_hub.py &

# 啟動你的 Agent
HUB_URL=http://localhost:9000 python -m agents.my_agent &

# 確認註冊
curl http://localhost:9000/agents

# 測試 dispatch
curl -X POST http://localhost:9000/dispatch \
  -H "Content-Type: application/json" \
  -d '{"message": "你的關鍵字", "chat_id": 0}'
```

## 參考

- `agents/claude_code/` — 複雜範例：CLI subprocess + JSON stream + 多輪 session 管理
- `core/base_agent.py` — BaseAgent 原始碼
- `core/models.py` — TaskRequest / AgentResult / TaskStatus 定義
- `core/sandbox.py` — Sandbox 安全限制
- `core/tool_registry.py` — @tool 裝飾器（用於 Claude API agent）
- `core/llm.py` — LLMClient（用於 Claude API agent）
