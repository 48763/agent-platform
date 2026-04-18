# 共用模組升級 — LLM 統一介面、Dashboard 框架、Agent 錯誤回報

## 概述

三個共用模組升級，讓未來新 Agent 只需寫業務邏輯：

1. **`core/llm.py` 升級** — 統一 LLM 介面，支援 Claude API 和 Gemini CLI，agent.yaml 擇一設定，啟動時檢測可用性
2. **`core/agent_dashboard.py`** — 共用 Dashboard 框架，agent 只需提供 `get_stats()` 回傳 dict
3. **Hub `/register_error`** — agent 啟動失敗時向 Hub 回報錯誤，Dashboard 顯示問題

---

## 1. LLM 統一介面

### agent.yaml 設定

```yaml
settings:
  # 簡寫（用預設模型）
  llm: claude

  # 或完整寫法
  llm:
    provider: gemini
    model: gemini-2.5-flash
```

### 預設模型

| provider | 預設 model |
|----------|-----------|
| `claude` | `claude-sonnet-4-20250514` |
| `gemini` | `gemini-2.5-flash` |

### 統一介面

```python
class LLMClient:
    provider: str   # "claude" or "gemini"
    model: str

    async def prompt(self, text: str) -> str:
        """單次 prompt → response。兩個 provider 都支援。"""

    async def run(self, system_prompt, messages, tools_schema, tool_executor, max_iterations=20) -> str:
        """Agentic loop with tool calling。
        Claude: 原生支援（現有邏輯）。
        Gemini: 僅支援 prompt 模式，不支援 tool calling，呼叫時拋 NotImplementedError。
        """
```

### 底層實作

**Claude provider：**
- 使用 `anthropic.AsyncAnthropic()` SDK
- `prompt()` → 呼叫 messages API，single turn，回傳 text
- `run()` → 現有 agentic loop 邏輯（tool calling）

**Gemini provider：**
- spawn `gemini -p "{prompt}" -m {model}` CLI process
- `prompt()` → 執行 CLI，回傳 stdout
- `run()` → 拋出 `NotImplementedError("Gemini provider does not support agentic loop")`
- timeout: 60 秒

### 啟動檢測

```python
async def create_llm_client(config: dict) -> LLMClient:
    """從 agent.yaml settings 建立 LLMClient。失敗拋 LLMInitError。"""
```

**Claude 檢測：**
1. 檢查 `ANTHROPIC_API_KEY` 環境變數存在
2. 嘗試建立 `anthropic.AsyncAnthropic()` client
3. 失敗 → 拋 `LLMInitError("Claude API key 未設定或無效")`

**Gemini 檢測：**
1. 執行 `which gemini` 確認 CLI 存在
2. 執行 `gemini -p "ping" -m {model}` 測試可用性
3. 失敗 → 拋 `LLMInitError("Gemini CLI 不可用: {detail}")`

### 工廠函數

```python
def parse_llm_config(settings: dict) -> tuple[str, str]:
    """解析 agent.yaml 的 llm 設定，回傳 (provider, model)。"""
    llm = settings.get("llm")
    if isinstance(llm, str):
        return llm, DEFAULT_MODELS[llm]
    if isinstance(llm, dict):
        provider = llm["provider"]
        model = llm.get("model", DEFAULT_MODELS[provider])
        return provider, model
    return None, None  # 沒設定 LLM
```

---

## 2. Dashboard 共用框架

### Agent 端介面

Agent 只需實作一個 async 函數：

```python
async def get_stats() -> dict:
    return {
        "title": "TG Transfer 統計",
        "counters": [
            ("儲存媒體", 128),
            ("標籤數", 15),
        ],
        "tables": [
            {
                "title": "標籤統計",
                "headers": ["標籤", "數量"],
                "rows": [("#教學", 30), ("#python", 20)],
            },
        ],
    }
```

### 框架提供

```python
from core.agent_dashboard import create_dashboard_handler

handler = create_dashboard_handler(get_stats)
app.router.add_get("/dashboard", handler)
```

`create_dashboard_handler(stats_fn)`:
- 接收一個 `async () -> dict` 函數
- 回傳 aiohttp handler
- 自動渲染 HTML：
  - `title` → 頁面標題
  - `counters` → 大數字卡片（橫排）
  - `tables` → 表格（每個 table 有標題、headers、rows）
- 樣式：深色主題（與 Hub Dashboard 一致）

### Stats dict 格式

```python
{
    "title": str,                              # 頁面標題
    "counters": list[tuple[str, int|str]],     # [(label, value), ...]
    "tables": list[{                           # 可選，多個表格
        "title": str,
        "headers": list[str],
        "rows": list[tuple],
    }],
}
```

---

## 3. Hub `/register_error` endpoint

### 端點

- **URL:** `POST /register_error`
- **Body:** `{"name": "agent-name", "error": "錯誤訊息"}`
- **不需登入**（跟 `/register` 和 `/heartbeat` 一樣）

### Hub 行為

- 在 registry 中記錄 agent 為 `error` 狀態，附帶錯誤訊息
- Agent 狀態從 online/offline/disabled 新增 `error`
- Dashboard 顯示為紅色，展示錯誤訊息
- 不需要 heartbeat（agent 已退出）
- 下次同名 agent 正常 `/register` 時，自動清除 error 狀態

### BaseAgent 改動

在 `run()` 中，`_init_services()` 之前先做 LLM 檢測：

```python
async def run(self):
    try:
        settings = self.config.get("settings", {})
        if settings.get("llm"):
            self.llm = await create_llm_client(settings)
    except LLMInitError as e:
        # 向 Hub 報告錯誤
        await self._register_error(str(e))
        logger.error(f"LLM init failed: {e}")
        sys.exit(1)
    await self._init_services()
    ...
```

`_register_error(error_msg)`:
- `POST {hub_url}/register_error` with `{"name": self.name, "error": error_msg}`
- 失敗也不影響（Hub 可能也沒啟動），只是 stdout 印錯誤

---

## 修改檔案

```
core/
├── llm.py              # 重寫：統一 LLM 介面 + Claude/Gemini provider + 檢測
├── agent_dashboard.py  # 新增：Dashboard HTML 渲染框架
├── base_agent.py       # 修改：加入 LLM 檢測 + _register_error

hub/
├── server.py           # 修改：新增 /register_error endpoint
├── registry.py         # 修改：支援 error 狀態
├── dashboard.py        # 修改：顯示 error 狀態 agent

agents/tg_transfer/
├── __main__.py         # 修改：改用 core/llm.py 取代直接 spawn gemini
├── dashboard.py        # 修改：改用 core/agent_dashboard.py 框架
```
