# Claude Code Agent

透過 Claude Code CLI 執行程式相關任務：寫程式、code review、修改檔案、執行指令。

## 功能

- 啟動 Claude Code CLI subprocess，以 `--output-format stream-json` 串流輸出
- 支援多輪對話（同一 task 維持同一個 CLI session）
- 解析 CLI JSON 事件：`done`、`need_approval`、`need_input`、`error`
- 可自訂 system prompt（`/data/prompts/system.txt`）

## 設定

**agent.yaml：**

```yaml
name: claude-code-agent
description: "使用 Claude Code 執行程式相關任務"
priority: 5
route_patterns:
  - "code|程式|review|修改檔案|修改程式|bug|fix|refactor|寫程式|code review"
```

**環境變數（`.env/claude-code-agent.env`）：**

```env
HUB_URL=http://hub:9000
AGENT_HOST=claude-code-agent
AGENT_PORT=8010
WORK_DIR=/workspace
```

## 首次認證

```bash
docker compose build claude-code-agent
docker compose up claude-code-agent -d
docker exec -it agent-claude-code-agent-1 sh
claude auth login
# 複製網址到瀏覽器 → 登入 → 貼回 code → exit
```

## 架構

```
agents/claude_code/
├── agent.yaml        # 路由設定
├── __main__.py       # ClaudeCodeAgent 入口 + handle_task
├── cli_session.py    # CLI subprocess 管理 + JSON stream 解析
└── Dockerfile
```

**handle_task 邏輯：**
- 有既存 session → 送使用者輸入到 CLI
- 無 session → 啟動新 CLI process
- CLI 回傳事件轉換為 `AgentResult`（DONE / NEED_APPROVAL / NEED_INPUT / ERROR）
