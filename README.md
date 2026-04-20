# Agent Platform

容器化 multi-agent 平台，透過 Telegram 接收訊息，Hub 智慧路由分配給對應的子 agent 執行任務。

## 架構

```
                          ┌───────────────────────────┐
                          │ Hub                        │       ┌──────────────────┐
Telegram ─┐               │ ├─ Gemini Router (flash)   │  WS   │ Agents           │
Discord  ─┼─▶ Gateway ═══▶│ ├─ Gemini Chat   (pro)    │◄═════▶│ ├─ claude-code   │
Line     ─┘      WS       │ ├─ TaskManager   (SQLite)  │       │ ├─ tg-transfer   │
                          │ └─ Dashboard               │       │ └─ ...           │
                          └───────────────────────────┘

═══ WebSocket 長連線（雙向通訊）
```

## Hub 運作原理

### 路由流程

```
訊息進來
  → Reply 某條 bot 訊息？ → 精準找到該對話，接續（0ms）
  → 有 agent 在等你回覆？ → 直接接續（0ms）
  → 關鍵字命中？ → 分配給對應 agent（0ms）
  → 以上都沒有 → Gemini flash 一次判斷：
      CONTINUE task_id → 接續進行中的對話
      ROUTE agent_name → 新任務交給 agent
      CHAT → Hub 用 Gemini pro 回覆
```

### Agent 註冊與生命週期

1. Agent 容器啟動 → LLM 認證檢測 → `_init_services()` 初始化
2. 向 Hub `POST /register` 一次性 HTTP 註冊（帶 name、description、url、route_patterns、priority、auth 狀態）
3. 建立 WebSocket 長連線到 Hub（`ws://hub:9000/ws/agent/{name}`）
4. 所有 task 派發和結果回報走 WS，WS ping/pong 取代 heartbeat
5. WS 斷線 → Hub 標記 agent 離線、關閉其所有進行中 task、通知 Gateway 用戶
6. Agent 自動重連（3 秒後重試）

**初始化失敗不會 crash** — agent 保持運行並向 Hub 回報錯誤狀態，Dashboard 可見問題原因。修正問題後重啟即恢復。

### Agent 狀態

| 狀態 | Dashboard 顯示 | 路由可選取 | 觸發條件 |
|------|---------------|-----------|---------|
| **online** | 綠色 | 是 | WS 已連線、認證通過、服務初始化成功 |
| **unauthenticated** | 顯示錯誤訊息 | 否 | LLM 未認證（無 API key 且未 CLI login） |
| **error** | 紅色+錯誤訊息 | 否 | 服務初始化失敗（如缺少設定、DB 連線失敗等） |
| **disabled** | 黃色 | 否 | 手動停用 |
| **offline** | 紅色 | 否 | WS 斷線 |

### 訊息處理

- **Reply 訊息** — 立即獨立處理，精準對應到特定 task
- **非 Reply 訊息** — 等待 5 秒收集，合併後一起處理（避免連發多條造成多個 task）

### 對話管理

- **SQLite 持久化** — 對話記錄存在 `data/hub/data/tasks.db`，重啟不遺失
- **Reply 接續** — 引用回覆 bot 訊息可精準回到該對話
- **智慧判斷接續** — Gemini flash 根據進行中對話的上下文判斷新訊息是否接續
- **所有 bot message_id 都記錄** — reply 任何一條 bot 回覆都能找到正確 task
- **`/clear`** — 手動關閉當前對話
- **Schema 自動遷移** — DB 結構變更時自動補欄位，不遺失資料

### 任務生命週期

```
working / waiting_input / waiting_approval
  ↓ 回覆完畢
done（已完成）── 可被路由選取、可 reply 接續
  ↓ 3 天未活動
archived（已封存）── 不可被路由選取、reply 可重新開啟
  ↓ 7 天未活動
closed（已關閉）── 不可被路由和 reply
  ↓ 7 天未活動
永久刪除
```

| 狀態 | 路由可選取 | Reply 可重開 |
|------|-----------|-------------|
| **working** | 是 | — |
| **waiting_input** | 是 | — |
| **waiting_approval** | 是 | — |
| **done** | 是 | 是 |
| **archived** | 否 | 是 |
| **closed** | 否 | 否 |

## Dashboard

瀏覽器訪問 `http://localhost:9000`

### 登入驗證

在 `.env/hub.env` 設定帳號密碼：

```env
DASHBOARD_USER=admin
DASHBOARD_PASS=your-password
```

- 設了密碼 → 訪問 Dashboard 需登入，session 保留 7 天
- 密碼留空 → 不需登入（開發模式）
- API 端點（register、heartbeat、dispatch）不受登入限制

### 功能

- **Agent 管理** — WS 連線狀態/離線/停用/未認證/錯誤、統計數據、關鍵字標籤、停用/啟用
- **Gateway 連線** — 顯示連線數量、每個 Gateway 的模式和監聽群組
- **對話紀錄** — Tab 篩選、搜尋、關閉/重開/刪除對話（關閉會透過 WS 送 cancel 給 agent）
- **統計概覽** — 在線 Agent 數、Gateway 連線數、處理中任務數、全部對話數
- **10 秒自動刷新**

## Agents

| Agent | 說明 | 文件 |
|-------|------|------|
| **Claude Code** | 透過 Claude Code CLI 執行程式任務 | [`agents/claude_code/README.md`](agents/claude_code/README.md) |
| **TG Transfer** | Telegram 聊天資源搬移（群組/頻道/用戶/bot 皆可）、媒體去重、搜尋、標籤 | [`agents/tg_transfer/README.md`](agents/tg_transfer/README.md) |

## 共用模組（core/）

所有 Agent 可直接引用，開發新 Agent 只需寫業務邏輯。

| 模組 | 用途 |
|------|------|
| `base_agent.py` | Agent 基底類別：WS 連線、Hub 註冊、LLM 檢測、錯誤回報 |
| `ws.py` | WebSocket 訊息協議：MsgType enum、ws_msg/ws_parse helper |
| `models.py` | 共用資料模型：`TaskRequest`（含 chat_id）、`AgentResult`、`TaskStatus`、`AgentInfo` |
| `config.py` | YAML 設定載入 |
| `sandbox.py` | 路徑/指令安全限制（per-agent 設定） |
| `llm.py` | 統一 LLM 介面：支援 Claude API + Gemini CLI，agent.yaml 擇一設定 |
| `tool_registry.py` | `@tool` 裝飾器 + Claude API tool schema 自動產生 |
| `agent_dashboard.py` | 共用 Dashboard 框架：agent 提供 stats dict，自動渲染 HTML |

## 快速開始

### 1. 複製設定檔

```bash
cp .env/example/*.env .env/
```

編輯各 env 檔填入你的設定。

### 2. 安裝本地開發環境

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r hub/requirements.txt -r gateway/requirements.txt
pip install -r agents/claude_code/requirements.txt -r agents/tg_transfer/requirements.txt
pip install pytest pytest-asyncio pytest-aiohttp
```

### 3. 首次認證

各服務需要在容器內完成認證，認證資料會保存在 `data/` 目錄下，container 重啟不需重新登入。

**Gateway（Telegram Userbot）：**

```bash
source .venv/bin/activate
export $(grep -v '^#' .env/gateway.env | grep -v '^$' | xargs)
DATA_DIR=data/gateway python -m gateway
# 輸入手機號碼 → 輸入 Telegram 驗證碼 → 看到 connected 後 Ctrl+C
```

**Hub（Gemini CLI）：**

```bash
docker compose build hub
docker compose up hub -d
docker exec -it agent-hub-1 sh
# 進入容器後：
gemini auth login
# 複製認證網址到本機瀏覽器開啟 → 登入 → 貼回 passkey → exit
```

**Agent LLM 認證（進容器手動登入）：**

```bash
# Claude Code Agent
docker exec -it agent-claude-code-agent-1 sh
claude auth login

# TG Transfer Agent
docker exec -it agent-tg-transfer-agent-1 sh
gemini auth login
```

### 4. 啟動

```bash
docker compose up -d
```

### 5. 確認狀態

```bash
docker compose ps
curl http://localhost:9000/agents
open http://localhost:9000
```

## 目錄結構

```
.
├── core/                          # 共用模組
│   ├── base_agent.py              #   Agent 基底類別（WS 連線）
│   ├── ws.py                      #   WebSocket 訊息協議
│   ├── config.py                  #   YAML 設定載入
│   ├── llm.py                     #   統一 LLM 介面（Claude/Gemini）
│   ├── agent_dashboard.py         #   共用 Dashboard 框架
│   ├── models.py                  #   共用資料模型
│   ├── sandbox.py                 #   路徑/指令沙盒
│   └── tool_registry.py           #   @tool 裝飾器
│
├── hub/                           # Hub 服務
│   ├── run.py                     #   入口
│   ├── server.py                  #   HTTP server + Dashboard
│   ├── ws_handler.py              #   WebSocket handler（agent + gateway）
│   ├── registry.py                #   Agent 在線管理（WS 連線狀態）
│   ├── router.py                  #   關鍵字比對
│   ├── task_manager.py            #   SQLite 對話管理
│   ├── gemini_fallback.py         #   Gemini 路由 + Chat
│   ├── dashboard.py               #   Web Dashboard
│   ├── auth.py                    #   登入驗證
│   ├── cli.py                     #   CLI 測試介面
│   └── requirements.txt           #   Hub 依賴
│
├── gateway/                       # Gateway 服務
│   ├── telegram_handler.py        #   Bot API 模式
│   ├── telegram_user_handler.py   #   Userbot 模式
│   └── __main__.py                #   入口
│
├── agents/                        # 子 Agent（各自有 README.md）
│   ├── claude_code/
│   └── tg_transfer/
│
├── tests/                         # 測試（151 tests）
├── docker-compose.yaml
└── AGENTS.md                      # Agent 開發共用指南
```

## 環境變數

各服務的 `.env` 範本在 `.env/example/`，複製後編輯：`cp .env/example/*.env .env/`

### Hub（`.env/hub.env`）

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `HUB_HOST` | `0.0.0.0` | 監聽地址 |
| `HUB_PORT` | `9000` | 監聽 port |
| `HUB_HEARTBEAT_TIMEOUT` | `30` | 相容保留（WS 連線用 ping/pong 取代） |
| `GEMINI_FAST_MODEL` | `gemini-2.5-flash` | 路由判斷用模型 |
| `GEMINI_DEFAULT_MODEL` | `gemini-2.5-pro` | 閒聊回覆用模型 |
| `DASHBOARD_USER` | — | Dashboard 帳號（留空不需登入） |
| `DASHBOARD_PASS` | — | Dashboard 密碼 |

### Gateway（`.env/gateway.env`）

| 變數 | 說明 |
|------|------|
| `HUB_URL` | Hub 地址 |
| `DATA_DIR` | 資料目錄（session 檔案） |
| `GATEWAY_MODE` | `userbot` 或 `bot` |
| `TELEGRAM_API_ID` | Telegram API ID（userbot） |
| `TELEGRAM_API_HASH` | Telegram API Hash（userbot） |
| `TELEGRAM_PHONE` | 手機號碼（userbot） |
| `TELEGRAM_BOT_TOKEN` | Bot token（bot mode） |
| `ALLOWED_CHATS` | 可選，限制回應的 chat ID（逗號分隔） |

### Agent 共用

| 變數 | 說明 |
|------|------|
| `HUB_URL` | Hub 地址 |
| `AGENT_HOST` | Agent 自身 hostname |
| `AGENT_PORT` | Agent 自身 port |
| `DATA_DIR` | 資料持久化目錄 |

### LLM 認證（設 API key 或進容器 CLI login，二擇一）

| 變數 | 說明 |
|------|------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `GEMINI_API_KEY` | Gemini API key |

### 自訂 Prompt

| 檔案 | 用途 |
|------|------|
| `data/hub/prompts/gemini_unified_router.txt` | flash 統一路由判斷 |
| `data/hub/prompts/gemini_default_reply.txt` | Hub 閒聊回覆 |

## API

### WebSocket 端點

| 端點 | 方向 | 說明 |
|------|------|------|
| `/ws/agent/{name}` | Agent → Hub | Agent 連入，雙向傳輸 task/result/progress/cancel |
| `/ws/gateway` | Gateway → Hub | Gateway 連入，雙向傳輸 dispatch/reply/progress |

**WS 訊息協議**（JSON，`type` 欄位區分）：

| type | 方向 | 用途 |
|------|------|------|
| `dispatch` | Gateway → Hub | 派發用戶訊息 |
| `reply` | Hub → Gateway | 回覆用戶 |
| `task` | Hub → Agent | 派發任務 |
| `result` | Agent → Hub | 任務結果 |
| `progress` | Agent → Hub → Gateway | 進度推送 |
| `cancel` | Hub → Agent | 終止任務 |
| `gw_register` | Gateway → Hub | Gateway 上報自身配置 |

### HTTP 端點（不需登入）

| 端點 | 方法 | 說明 |
|------|------|------|
| `/register` | POST | Agent 一次性註冊 |
| `/register_error` | POST | Agent 啟動失敗回報 |
| `/agents` | GET | 列出在線 agent |
| `/dispatch` | POST | 分配訊息（保留作為相容 fallback） |
| `/set_message_id` | POST | 回報 bot message_id |

### Dashboard 端點（需登入）

| 端點 | 方法 | 說明 |
|------|------|------|
| `/` | GET | Dashboard 網頁 |
| `/dashboard/tasks` | GET | 取得對話列表 |
| `/dashboard/task/{id}/close` | POST | 關閉對話（同時透過 WS 送 cancel 給 agent） |
| `/dashboard/task/{id}/reopen` | POST | 重開對話 |
| `/dashboard/task/{id}/delete` | POST | 刪除對話 |
| `/dashboard/agents` | GET | 取得 agent 資訊（含 WS 連線狀態） |
| `/dashboard/gateways` | GET | 取得 Gateway 連線資訊 |
| `/dashboard/agent/{name}/disable` | POST | 停用 agent |
| `/dashboard/agent/{name}/enable` | POST | 啟用 agent |

## 測試

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## 工具

```bash
# 查詢 Telegram chat ID
source .venv/bin/activate
export $(grep -v '^#' .env/gateway.env | grep -v '^$' | xargs)
DATA_DIR=data/gateway python gateway/list_chats.py
```
