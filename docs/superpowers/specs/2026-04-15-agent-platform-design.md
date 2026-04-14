# Agent Platform Design Spec

## Overview

本機 multi-agent 平台，透過 Telegram Bot 接收使用者指令，根據路由規則分配給對應的子 agent 執行任務。子 agent 為獨立 process，啟動時向主服務註冊，透過 HTTP 通訊。

## Architecture

```
Telegram
   ↓
┌──────────────────────────────┐
│  Hub（主服務）                 │
│  ├─ TG Bot    — 接收/回傳訊息 │
│  ├─ Router    — 分配任務      │
│  └─ Registry  — 管理 agent   │
└──────────┬───────────────────┘
           │ HTTP
     ┌─────┼─────┐
     ↓     ↓     ↓
  Agent A  B     C
```

### 通訊方式

- Hub ↔ Agent：HTTP API
- Agent 啟動時 POST 到 Hub 註冊，定期 heartbeat
- Hub 透過 POST 發送任務給 Agent，Agent 同步回應結果

### 路由策略

混合模式：
1. 先用關鍵字比對（agent.yaml 中定義 route_patterns）
2. 比對不到時，呼叫 Claude API 判斷意圖，從已註冊 agent 中選擇

## Project Structure

```
agent-platform/
├── core/
│   ├── __init__.py
│   ├── base_agent.py       # BaseAgent 基底類別
│   ├── sandbox.py           # 路徑/指令沙盒
│   ├── llm.py               # Claude API 封裝 + agentic loop
│   ├── tool_registry.py     # @tool 裝飾器 + 收集機制
│   └── config.py            # YAML 設定載入
│
├── hub/
│   ├── __init__.py
│   ├── server.py            # Hub HTTP server（接收 agent 註冊、健康檢查）
│   ├── registry.py          # Agent registry（在線狀態管理）
│   ├── router.py            # 任務路由（關鍵字 + Claude fallback）
│   ├── bot.py               # Telegram Bot 整合
│   └── cli.py               # CLI 測試介面（開發用，不經 TG）
│
├── agents/
│   └── weather/
│       ├── agent.yaml        # 設定：名稱、描述、路由規則、沙盒
│       ├── tools.py          # 天氣查詢 tool（wttr.in）
│       └── prompts.py        # system prompt
│
├── config.yaml               # 全域設定（Hub port、Claude API key 位置等）
├── requirements.txt
└── README.md
```

## Core Modules

### BaseAgent (`core/base_agent.py`)

子 agent 的基底類別，負責：
- 載入 agent.yaml 設定
- 啟動 HTTP server 接收任務
- 向 Hub 註冊（POST /register）
- 定期 heartbeat（POST /heartbeat）
- 收到任務後進入 agentic loop

```python
class BaseAgent:
    name: str
    config: dict
    sandbox: Sandbox
    tools: list
    system_prompt: str

    async def run(self)           # 啟動 agent
    async def handle_task(self, task) -> AgentResult  # 處理任務
```

### Sandbox (`core/sandbox.py`)

應用層安全限制：
- `allowed_dirs`：允許存取的目錄白名單
- `denied_patterns`：檔案黑名單（如 *.env, *.pem）
- `allowed_commands`：可執行的指令白名單
- `writable`：是否允許寫入
- `network.allow`：允許存取的域名白名單

所有檔案操作和指令執行都必須經過 Sandbox 檢查。

### LLM (`core/llm.py`)

Claude API 封裝：
- 包裝 Anthropic SDK 呼叫
- 實作 agentic loop：Claude 回應 → 檢查是否有 tool call → 執行 tool → 回傳結果 → 繼續，直到得到最終回答
- 處理 tool call 的序列化/反序列化

### ToolRegistry (`core/tool_registry.py`)

- `@tool(description=...)` 裝飾器標記函式為 tool
- 自動從 tools.py 收集所有 tool
- 轉換為 Claude API 的 tool schema 格式

## Hub Modules

### Registry (`hub/registry.py`)

- 維護已註冊 agent 列表（name, description, url, capabilities, last_heartbeat）
- Agent 註冊：POST /register
- Heartbeat：POST /heartbeat
- 超時未 heartbeat 標記為離線

### Router (`hub/router.py`)

1. 收到使用者訊息
2. 比對所有在線 agent 的 route_patterns
3. 命中 → 分配給該 agent
4. 未命中 → 呼叫 Claude，提供所有在線 agent 的 description，讓 Claude 選擇
5. Claude 也無法判斷 → 回覆使用者「無法處理」

### Bot (`hub/bot.py`)

- python-telegram-bot 套件
- 接收訊息 → 交給 Router → 等待結果 → 回傳給使用者
- 支援追問：agent 回傳 need_input 時，轉發問題給使用者，收到回答後繼續

### CLI (`hub/cli.py`)

開發用的 CLI 介面，模擬 TG Bot 行為，直接在 terminal 輸入訊息測試。

## Agent: Weather

### agent.yaml

```yaml
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

### Tools

- `get_weather(city: str) -> str`：呼叫 wttr.in API 取得天氣資訊

### System Prompt

天氣查詢助手，收到城市名稱後查詢天氣並以簡潔中文回報。若使用者未指定城市，主動詢問。

## API Contracts

### Agent → Hub 註冊

```
POST {hub_url}/register
{
    "name": "weather-agent",
    "description": "查詢天氣資訊",
    "url": "http://localhost:8001",
    "route_patterns": ["天氣|weather|氣溫"],
    "capabilities": ["get_weather"]
}
```

### Hub → Agent 派發任務

```
POST {agent_url}/task
{
    "task_id": "uuid",
    "content": "台北今天天氣如何？",
    "conversation_history": [...]
}
```

### Agent 回應

```json
// 完成
{"status": "done", "message": "台北目前 25°C，多雲..."}

// 需要追問（純文字）
{"status": "need_input", "message": "你想查哪個城市的天氣？"}

// 需要追問（帶選項，TG Bot 會用 Inline Keyboard 顯示按鈕）
{
    "status": "need_input",
    "message": "Gemini 問你要用哪個模型？",
    "options": ["gemini-2.5-pro", "gemini-2.5-flash"]
}

// 錯誤
{"status": "error", "message": "天氣服務暫時無法使用"}
```

### Heartbeat

```
POST {hub_url}/heartbeat
{"name": "weather-agent"}
```

## Implementation Order

1. Core 框架：config, tool_registry, sandbox, llm, base_agent
2. Hub：registry, router, server, cli
3. Weather agent：agent.yaml, tools, prompts
4. 整合測試：CLI 模式跑通完整流程
5. Telegram Bot：bot.py，接上 TG

## Dependencies

- `anthropic` — Claude API
- `aiohttp` — HTTP server/client（Hub 和 Agent 都用）
- `python-telegram-bot` — TG Bot
- `pyyaml` — YAML 設定載入

## Error Handling

- Agent 離線：Router 回報「該 agent 目前不在線」
- Claude API 失敗：重試一次，仍失敗則回報錯誤
- Tool 執行失敗：錯誤訊息回傳給 Claude，讓它決定如何處理
- Sandbox 違規：拋出 PermissionError，不執行該操作，回報給使用者
