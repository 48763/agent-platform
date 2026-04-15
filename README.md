# Agent Platform

容器化 multi-agent 平台，透過 Telegram 接收訊息，Hub 路由分配給對應的子 agent 執行任務。

## 架構

```
Telegram (帳號A) ──訊息──▶ Gateway (帳號B) ──▶ Hub ──▶ Agents
                                                 │
                                     ┌───────────┼───────────┐
                                     ↓           ↓           ↓
                              claude-code   weather    (更多 agent)
                              (Claude CLI)  (wttr.in)
```

### 服務

| 服務 | 職責 | 容器 |
|------|------|------|
| **Gateway** | 接收 Telegram 訊息，轉發給 Hub | `gateway` |
| **Hub** | 路由分配、agent 註冊管理、多輪對話追蹤 | `hub` |
| **Claude Code Agent** | 使用 Claude Code CLI 執行程式任務 | `claude-code-agent` |
| **Weather Agent** | 查詢天氣（wttr.in） | `weather-agent` |

### 路由流程

1. **關鍵字比對** — 根據 agent 的 `route_patterns` 匹配（priority 高的先比對）
2. **Gemini CLI 判斷** — 關鍵字沒命中時，Gemini 分析該分配給哪個 agent
3. **Hub 直接回覆** — 都沒有合適 agent 時，Hub 用 Gemini 直接回覆

## 快速開始

### 1. 複製設定檔

```bash
cp .env/example/*.env .env/
```

編輯各 env 檔填入你的設定。

### 2. 首次認證

**Gateway（Telegram Userbot）：**

```bash
# 本地產生 session
source .venv/bin/activate
export $(grep -v '^#' .env/gateway.env | grep -v '^$' | xargs)
SESSION_PATH=data/gateway/bot_session python -m gateway
# 輸入驗證碼後 Ctrl+C
```

**Hub（Gemini CLI）：**

```bash
docker compose build hub
docker compose up hub -d
docker exec -it agent-hub-1 gemini auth login
```

**Claude Code Agent：**

```bash
docker compose build claude-code-agent
docker compose up claude-code-agent -d
docker exec -it agent-claude-code-agent-1 claude auth login
```

### 3. 啟動

```bash
docker compose up -d
```

### 4. 確認狀態

```bash
# 查看所有服務
docker compose ps

# 查看已註冊的 agent
curl http://localhost:9000/agents
```

## 目錄結構

```
.
├── core/                         # 共用模組
│   ├── base_agent.py             #   BaseAgent 基底類別
│   ├── config.py                 #   YAML 設定載入
│   ├── llm.py                    #   Claude API 封裝 + agentic loop
│   ├── models.py                 #   共用資料模型
│   ├── sandbox.py                #   路徑/指令沙盒
│   └── tool_registry.py          #   @tool 裝飾器
│
├── hub/                          # Hub 服務
│   ├── Dockerfile
│   ├── server.py                 #   HTTP server（/register, /heartbeat, /dispatch）
│   ├── registry.py               #   Agent 在線管理
│   ├── router.py                 #   三層路由（關鍵字 → Gemini → 預設回覆）
│   ├── task_manager.py           #   多輪對話狀態追蹤
│   ├── gemini_fallback.py        #   Gemini CLI 路由 + 預設回覆
│   └── cli.py                    #   CLI 測試介面
│
├── gateway/                      # Gateway 服務
│   ├── Dockerfile
│   ├── telegram_handler.py       #   Bot API 模式（@BotFather）
│   ├── telegram_user_handler.py  #   Userbot 模式（Telethon）
│   ├── list_chats.py             #   查詢 chat ID 工具
│   └── __main__.py               #   入口（根據 GATEWAY_MODE 選擇模式）
│
├── agents/                       # 子 Agent
│   ├── claude_code/              #   Claude Code CLI agent
│   │   ├── Dockerfile
│   │   ├── agent.yaml            #     設定（route_patterns, priority）
│   │   ├── cli_session.py        #     CLI subprocess 管理 + JSON stream 解析
│   │   └── __main__.py           #     入口
│   └── weather/                  #   天氣查詢 agent
│       ├── Dockerfile
│       ├── agent.yaml
│       ├── tools.py              #     wttr.in 查詢
│       └── prompts.py            #     system prompt
│
├── data/                         # 持久化資料（gitignore）
│   ├── hub/
│   │   ├── .gemini/              #     Gemini CLI 設定 + GEMINI.md
│   │   └── prompts/              #     自訂 prompt 模板
│   ├── claude-code-agent/
│   │   ├── .claude/              #     Claude Code 設定 + CLAUDE.md
│   │   ├── .claude.json          #     認證資料
│   │   └── prompts/              #     自訂 system prompt
│   └── gateway/
│       └── bot_session.session   #     Telegram 登入 session
│
├── .env/                         # 環境變數
│   ├── example/                  #   範本（tracked in git）
│   │   ├── hub.env
│   │   ├── gateway.env
│   │   ├── claude-code-agent.env
│   │   └── weather-agent.env
│   ├── hub.env                   #   實際設定（gitignore）
│   ├── gateway.env
│   └── ...
│
├── tests/                        # 測試（49 tests）
├── docker-compose.yaml
├── config.yaml
├── requirements.txt
└── run_hub.py                    # Hub 入口
```

## 設定

### Gateway 模式

在 `.env/gateway.env` 設定 `GATEWAY_MODE`：

- **`userbot`** — 用個人帳號（Telethon），看起來像真人
- **`bot`** — 用 @BotFather 建立的 Bot

### 新增 Agent

1. 建立 `agents/your_agent/` 目錄
2. 寫 `agent.yaml`（name, description, priority, route_patterns）
3. 實作 `__main__.py`（繼承 `BaseAgent`）
4. 建立 `Dockerfile`
5. 在 `docker-compose.yaml` 加上服務
6. `docker compose up your-agent -d`

agent 啟動後會自動向 Hub 註冊。

### 自訂 Prompt

修改 `data/` 下對應的 prompt 檔案，不需要重新 build container，重啟服務即可生效。

## 測試

```bash
source .venv/bin/activate
pip install pytest pytest-asyncio pytest-aiohttp
python -m pytest tests/ -v
```

## API

| 端點 | 方法 | 說明 |
|------|------|------|
| `/register` | POST | Agent 註冊 |
| `/heartbeat` | POST | Agent 心跳 |
| `/agents` | GET | 列出在線 agent |
| `/dispatch` | POST | 分配訊息給 agent |
