# Agent Platform Design Spec

## Overview

本機 multi-agent 平台，透過 Telegram Bot 接收使用者指令，根據路由規則分配給對應的子 agent 執行任務。子 agent 為獨立 process，啟動時向主服務註冊，透過 HTTP 通訊。

## Architecture

```
Telegram ─┐
Discord  ─┼──▶ Gateway（接收訊息 + 統一格式）──▶ Hub（路由 + 分配）──▶ Agents
Line     ─┘         container                      container           containers
```

### 服務分層

- **Gateway**：訊息入口，負責接收各通訊軟體訊息、統一格式、轉發給 Hub、回傳結果給使用者
- **Hub**：核心路由服務，負責 agent 註冊管理、任務路由分配、多輪對話狀態追蹤
- **Agent**：各功能 agent，獨立 container，啟動時向 Hub 註冊

### 部署方式

- 每個服務為獨立 Docker container
- 使用 docker-compose 編排
- 服務間透過 Docker internal network 通訊（不需 expose port 到 host）
- Base image: python:3.12-alpine

### 通訊方式

- Gateway → Hub：HTTP API（POST /dispatch）
- Hub ↔ Agent：HTTP API（註冊、heartbeat、任務派發）
- Agent 啟動時 POST 到 Hub 註冊，定期 heartbeat

### 路由策略

混合模式：
1. 先用關鍵字比對（agent.yaml 中定義 route_patterns）
2. 比對不到時，呼叫 Claude API 判斷意圖，從已註冊 agent 中選擇

## Project Structure

```
agent-platform/
├── core/                      # 共用模組（所有服務都引用）
│   ├── __init__.py
│   ├── base_agent.py          # BaseAgent 基底類別
│   ├── sandbox.py             # 路徑/指令沙盒
│   ├── llm.py                 # Claude API 封裝 + agentic loop
│   ├── tool_registry.py       # @tool 裝飾器 + 收集機制
│   ├── models.py              # 共用資料模型
│   └── config.py              # YAML 設定載入
│
├── gateway/                   # Gateway 服務（訊息入口）
│   ├── __init__.py
│   ├── telegram_handler.py    # Telegram 訊息處理
│   ├── server.py              # Gateway HTTP server + 通訊軟體整合
│   └── Dockerfile
│
├── hub/                       # Hub 服務（路由 + 分配）
│   ├── __init__.py
│   ├── server.py              # Hub HTTP server
│   ├── registry.py            # Agent registry
│   ├── router.py              # 任務路由
│   ├── task_manager.py        # 多輪互動任務狀態管理
│   ├── cli.py                 # CLI 測試介面
│   └── Dockerfile
│
├── agents/                    # 各子 agent
│   └── weather/
│       ├── agent.yaml
│       ├── tools.py
│       ├── prompts.py
│       ├── __main__.py
│       └── Dockerfile
│
├── config.yaml
├── requirements.txt
├── docker-compose.yaml
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

## Gateway Service

獨立服務，負責接收各通訊軟體訊息並轉發給 Hub。

### Gateway Server (`gateway/server.py`)

- 啟動各通訊軟體 handler
- 統一訊息格式後 POST 到 Hub /dispatch
- 接收 Hub 回應後轉發回對應通訊軟體

### Telegram Handler (`gateway/telegram_handler.py`)

- python-telegram-bot 套件
- 接收訊息 → 轉發給 Hub → 回傳結果
- 支援 Inline Keyboard（options / need_approval）
- 未來可新增 Discord、Line 等 handler，不需修改 Hub

### TaskManager (`hub/task_manager.py`)

管理多輪互動的任務狀態，讓同一個對話的追問和權限確認能接續：

- 建立 Task：記錄 task_id、agent_name、chat_id、conversation_history
- 狀態流轉：working → waiting_input / waiting_approval → working → ... → done
- 使用者回應時，找到對應 Task，append 到 history，再發給 Agent 繼續
- need_approval 超時未回應時，自動回傳「拒絕」給 Agent

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

// 權限確認（敏感操作需使用者授權，TG 用醒目提示 + 按鈕）
{
    "status": "need_approval",
    "message": "Gemini 想要執行 rm -rf dist/，是否允許？",
    "action": "run_command: rm -rf dist/",
    "options": ["允許", "拒絕"]
}

// 錯誤
{"status": "error", "message": "天氣服務暫時無法使用"}
```

**Status 說明：**

| status | 用途 | 拒絕/超時行為 | TG 呈現 |
|--------|------|-------------|---------|
| done | 任務完成 | — | 一般訊息 |
| need_input | 補充資訊 | 等待 | 一般訊息或按鈕 |
| need_approval | 授權敏感操作 | 預設拒絕，agent 跳過該操作繼續 | ⚠️ 醒目提示 + 按鈕 |
| error | 執行失敗 | — | 錯誤訊息 |

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
