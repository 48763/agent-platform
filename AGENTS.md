# Agent Platform 開發指引

你正在一個容器化 multi-agent 平台中工作。這份文件讓你快速理解專案結構並開始開發。

## 專案概述

透過 Telegram 接收使用者訊息，Hub 智慧路由分配給對應的子 Agent 處理。每個 Agent 是獨立 Docker container。

```
Telegram → Gateway → Hub → Agents
                      │
              ┌───────┤
              ↓       ↓
           Router   Chat      ← Hub 內建（Gemini CLI）
                      
           Agents:
           └─ claude-code-agent ← Claude Code CLI
           └─ (你要開發的 agent)
```

## 快速開始開發 Agent

**必讀：** `docs/agent-development.md` — 完整的 Agent 開發指南

**核心概念：** Agent 只需實作一個方法 `handle_task(task) → AgentResult`

```python
from core.base_agent import BaseAgent
from core.models import AgentResult, TaskRequest, TaskStatus

class MyAgent(BaseAgent):
    async def handle_task(self, task: TaskRequest) -> AgentResult:
        return AgentResult(status=TaskStatus.DONE, message="完成")
```

## 關鍵檔案

| 路徑 | 用途 |
|------|------|
| `core/base_agent.py` | Agent 基底類別，繼承它 |
| `core/models.py` | TaskRequest、AgentResult、TaskStatus 定義 |
| `core/sandbox.py` | 路徑/指令安全限制 |
| `core/tool_registry.py` | @tool 裝飾器（用於 Claude API） |
| `core/llm.py` | Claude API 封裝 + agentic loop |
| `agents/claude_code/` | 複雜 Agent 範例（CLI subprocess） |
| `agents/tg_transfer/` | TG 資源搬移 Agent（媒體去重、搜尋、標籤、存活檢查） |
| `docs/agent-development.md` | 完整開發文件 |

## Agent 回傳狀態

| status | 用途 |
|--------|------|
| `TaskStatus.DONE` | 任務完成 |
| `TaskStatus.NEED_INPUT` | 需要使用者提供更多資訊 |
| `TaskStatus.NEED_APPROVAL` | 需要使用者授權敏感操作 |
| `TaskStatus.ERROR` | 執行失敗 |

## 建立 Agent 的步驟

1. 建立 `agents/your_agent/` 目錄
2. 寫 `agent.yaml`（name、description、priority、route_patterns）
3. 寫 `__main__.py`（繼承 BaseAgent，實作 handle_task）
4. 寫 `Dockerfile`
5. 加到 `docker-compose.yaml`
6. 建立 `.env/your-agent.env`

詳見 `docs/agent-development.md`。

## 規範

- 語言：Python
- commit 不加 Co-Authored-By
- 使用繁體中文回覆使用者
- priority 數字越小越優先匹配
- Agent 不需管理對話狀態，Hub 會帶完整 conversation_history
