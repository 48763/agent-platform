# Agent Platform

容器化 multi-agent 平台，透過 Telegram 接收訊息，Hub 智慧路由分配給對應的子 agent 執行任務，支援多輪對話接續與完整的任務生命週期管理。

## 架構

```
                          ┌───────────────────────────┐
                          │ Hub                        │       ┌──────────────────┐
Telegram ─┐               │ ├─ Gemini Router (flash)   │       │ Agents           │
Discord  ─┼─▶ Gateway ──▶ │ ├─ Gemini Chat   (pro)    │──────▶│ ├─ claude-code   │
Line     ─┘               │ ├─ TaskManager   (SQLite)  │       │ └─ ...           │
                          │ └─ Dashboard               │       └──────────────────┘
                          └───────────────────────────┘
```

### 服務

| 服務 | 職責 | 容器 |
|------|------|------|
| **Gateway** | 接收 Telegram 訊息，轉發給 Hub | `gateway` |
| **Hub** | 智慧路由、Gemini 閒聊、對話管理、agent 註冊、Dashboard | `hub` |
| **Claude Code Agent** | 使用 Claude Code CLI 執行程式任務 | `claude-code-agent` |

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

| 狀態 | 路由可選取 | Reply 可重開 | 說明 |
|------|-----------|-------------|------|
| **working** | 是 | — | 正在處理中 |
| **waiting_input** | 是 | — | Agent 等你回覆 |
| **waiting_approval** | 是 | — | Agent 等你授權 |
| **done** | 是 | 是 | 已完成，仍可接續 |
| **archived** | 否 | 是 | 封存，reply 可重開 |
| **closed** | 否 | 否 | 已關閉，後台可重開 |

## Dashboard

瀏覽器訪問 `http://localhost:9000`

- **Agent 列表** — 在線 agent、優先級、關鍵字
- **對話紀錄** — 全部 / 處理中 / 已完成 / 已封存 / 已關閉
- **操作** — 關閉進行中的對話、重新開啟已關閉的對話、刪除對話
- **顯示** — 處理的 agent、來源通訊軟體、訊息數、更新時間、對話內容
- **自動刷新** — 10 秒自動更新

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
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-aiohttp
```

### 3. 首次認證

各服務需要在容器內完成認證，認證資料會保存在 `data/` 目錄下，container 重啟不需重新登入。

**Gateway（Telegram Userbot）：**

```bash
source .venv/bin/activate
export $(grep -v '^#' .env/gateway.env | grep -v '^$' | xargs)
SESSION_PATH=data/gateway/bot_session python -m gateway
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

**Claude Code Agent：**

```bash
docker compose build claude-code-agent
docker compose up claude-code-agent -d
docker exec -it agent-claude-code-agent-1 sh
# 進入容器後：
claude auth login
# 複製認證網址到本機瀏覽器開啟 → 登入 → 貼回 code → exit
```

### 4. 啟動

```bash
docker compose up -d
```

### 5. 確認狀態

```bash
# 查看服務
docker compose ps

# 查看已註冊 agent
curl http://localhost:9000/agents

# 開啟 Dashboard
open http://localhost:9000
```

## 目錄結構

```
.
├── core/                          # 共用模組
│   ├── base_agent.py              #   BaseAgent 基底類別（HTTP server + 自動重註冊）
│   ├── config.py                  #   YAML 設定載入
│   ├── llm.py                     #   Claude API 封裝 + agentic loop
│   ├── models.py                  #   共用資料模型（AgentResult, TaskRequest, AgentInfo）
│   ├── sandbox.py                 #   路徑/指令沙盒
│   └── tool_registry.py           #   @tool 裝飾器 + schema 產生
│
├── hub/                           # Hub 服務
│   ├── Dockerfile
│   ├── server.py                  #   HTTP server + dispatch 路由邏輯
│   ├── registry.py                #   Agent 在線管理（heartbeat）
│   ├── router.py                  #   關鍵字比對
│   ├── task_manager.py            #   SQLite 對話管理（生命週期 + 自動遷移）
│   ├── gemini_fallback.py         #   Gemini 統一路由 + Chat 回覆
│   ├── dashboard.py               #   Web Dashboard
│   └── cli.py                     #   CLI 測試介面
│
├── gateway/                       # Gateway 服務
│   ├── Dockerfile
│   ├── telegram_handler.py        #   Bot API 模式（@BotFather）
│   ├── telegram_user_handler.py   #   Userbot 模式（Telethon + 5秒合併）
│   ├── list_chats.py              #   查詢 chat ID 工具
│   └── __main__.py                #   入口（GATEWAY_MODE 切換模式）
│
├── agents/                        # 子 Agent
│   └── claude_code/               #   Claude Code CLI agent
│       ├── Dockerfile
│       ├── agent.yaml             #     設定（route_patterns, priority）
│       ├── cli_session.py         #     CLI subprocess + JSON stream 解析
│       └── __main__.py            #     入口
│
├── data/                          # 持久化資料（gitignore）
│   ├── hub/
│   │   ├── .gemini/               #     Gemini CLI 設定 + GEMINI.md
│   │   ├── data/                  #     tasks.db（SQLite 對話記錄）
│   │   └── prompts/               #     自訂 prompt 模板
│   │       ├── gemini_unified_router.txt
│   │       └── gemini_default_reply.txt
│   ├── claude-code-agent/
│   │   ├── .claude/               #     Claude Code 設定 + CLAUDE.md
│   │   ├── .claude.json           #     認證資料
│   │   └── prompts/
│   │       └── system.txt         #     Claude Code system prompt
│   └── gateway/
│       └── bot_session.session    #     Telegram 登入 session
│
├── .env/                          # 環境變數
│   ├── example/                   #   範本（tracked in git）
│   │   ├── hub.env
│   │   ├── gateway.env
│   │   └── claude-code-agent.env
│   ├── hub.env                    #   實際設定（gitignore）
│   ├── gateway.env
│   └── claude-code-agent.env
│
├── tests/                         # 測試（50 tests）
├── docker-compose.yaml
├── config.yaml
├── requirements.txt
└── run_hub.py                     # Hub 入口
```

## 設定

### Gateway 模式

在 `.env/gateway.env` 設定 `GATEWAY_MODE`：

- **`userbot`** — 用個人帳號（Telethon），看起來像真人
- **`bot`** — 用 @BotFather 建立的 Bot

### Gemini 模型

在 `.env/hub.env` 設定：

```env
GEMINI_FAST_MODEL=gemini-2.5-flash    # 路由判斷用（快）
GEMINI_DEFAULT_MODEL=gemini-2.5-pro   # 閒聊回覆用（好）
```

### 新增 Agent

1. 建立 `agents/your_agent/` 目錄
2. 寫 `agent.yaml`：

```yaml
name: your-agent
description: "你的 agent 描述"
priority: 5               # 數字越大，關鍵字比對越優先
route_patterns:
  - "關鍵字1|關鍵字2"
sandbox:
  allowed_dirs: []
  writable: false
```

3. 實作 `__main__.py`（繼承 `BaseAgent`，實作 `handle_task`）
4. 建立 `Dockerfile`
5. 在 `docker-compose.yaml` 加上服務
6. `docker compose up your-agent -d`

agent 啟動後會自動向 Hub 註冊。Hub 重啟後 agent 會透過 heartbeat 自動重新註冊。

### 自訂 Prompt

修改 `data/` 下對應的 prompt 檔案，重啟服務即可生效，不需重新 build。

| 檔案 | 用途 |
|------|------|
| `data/hub/prompts/gemini_unified_router.txt` | flash 統一路由判斷的 prompt |
| `data/hub/prompts/gemini_default_reply.txt` | Hub 閒聊回覆的 prompt |
| `data/claude-code-agent/prompts/system.txt` | Claude Code 的 system prompt |

## 測試

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## API

### Hub 端點

| 端點 | 方法 | 說明 |
|------|------|------|
| `/` | GET | Dashboard 網頁介面 |
| `/register` | POST | Agent 註冊 |
| `/heartbeat` | POST | Agent 心跳（404 時自動重新註冊） |
| `/agents` | GET | 列出在線 agent |
| `/dispatch` | POST | 分配訊息（支援 `reply_to_message_id`、`source`） |
| `/set_message_id` | POST | Gateway 回報 bot 回覆的 message_id |

### Dashboard 端點

| 端點 | 方法 | 說明 |
|------|------|------|
| `/dashboard/tasks` | GET | 取得最近 50 筆對話 |
| `/dashboard/task/{id}/close` | POST | 關閉對話 |
| `/dashboard/task/{id}/reopen` | POST | 重新開啟已關閉的對話（→ done） |
| `/dashboard/task/{id}/delete` | POST | 永久刪除對話 |

## 工具

```bash
# 查詢 Telegram chat ID
source .venv/bin/activate
export $(grep -v '^#' .env/gateway.env | grep -v '^$' | xargs)
SESSION_PATH=data/gateway/bot_session python gateway/list_chats.py
```
