# WebSocket 通訊層 + tg-transfer 傳輸修復

日期：2026-04-19

## 背景

目前所有 inter-service 通訊都是 HTTP request-response。Gateway→Hub→Agent 的同步呼叫鏈在長時間任務（如批次轉存 800MB+ 影片）時會 timeout（aiohttp 預設 300 秒），導致 Gateway 回傳空的「處理失敗:」錯誤（`asyncio.TimeoutError` 的 str 為空）。

此外，tg-transfer 上傳影片時未帶 metadata，導致 Telegram 顯示 0:00 時長和錯誤比例。

## 改動範圍

### 一、tg-transfer 傳輸修復

#### 1.1 影片上傳帶 metadata

**問題**：`transfer_engine.py:136` 的 `send_file` 沒有帶任何影片屬性。

**修復**：上傳前用 ffprobe 提取 duration、width、height，帶入 Telethon 的 `DocumentAttributeVideo`：

```python
from telethon.tl.types import DocumentAttributeVideo

attrs = [DocumentAttributeVideo(
    duration=duration,
    w=width,
    h=height,
    supports_streaming=True,
)]
result = await self.client.send_file(
    target_entity, path, caption=message.text,
    attributes=attrs,
    supports_streaming=True,
)
```

需要新增 `ffprobe_metadata(path)` helper（在 `hasher.py` 或獨立的 `media_utils.py`），回傳 `{"duration": int, "width": int, "height": int}`。

Album 上傳同理：每個影片檔案都要帶各自的 metadata。

#### 1.2 下載 part size 最大化

將 Telethon 下載的 part size 設為 API 允許的最大值（512KB 或 1MB），減少 round trip。

#### 1.3 Album 並行下載

Album 內的多媒體改為 `asyncio.gather` 並行下載，取代現有的 sequential for loop。

#### 1.4 Album 原子性

Album 內任何一個媒體下載失敗或上傳失敗 → 整則 album 標記為 failed，不上傳任何內容。

現有問題：下載失敗的媒體被跳過，可能只上傳部分 album。

---

### 二、WebSocket 通訊層

#### 2.1 連線模型

所有連線都是 client → Hub：

- **Agent → Hub**：Agent 啟動後 HTTP `/register` 一次性註冊，然後建立 WS 連線到 `ws://hub:{port}/ws/agent/{name}`。每個 agent 一條共用 WS，所有 task 走這條。
- **Gateway → Hub**：Gateway 啟動後建立 WS 連線到 `ws://hub:{port}/ws/gateway`。一條共用 WS，所有訊息走這條。

Hub 是純 server，不主動對外連。

#### 2.2 WS 訊息協議

所有 WS 訊息為 JSON，用 `type` 欄位區分：

**Gateway → Hub：**

| type | 用途 | 欄位 |
|------|------|------|
| `dispatch` | 派發用戶訊息 | `chat_id`, `message`, `reply_to_message_id?`, `metadata?` |

**Hub → Gateway：**

| type | 用途 | 欄位 |
|------|------|------|
| `reply` | 回覆用戶 | `chat_id`, `task_id`, `message`, `status`, `options?` |
| `progress` | 任務進度推送 | `chat_id`, `task_id`, `message` |

**Hub → Agent：**

| type | 用途 | 欄位 |
|------|------|------|
| `task` | 派發任務 | `task_id`, `content`, `conversation_history`, `chat_id` |
| `cancel` | 終止任務 | `task_id` |

**Agent → Hub：**

| type | 用途 | 欄位 |
|------|------|------|
| `result` | 任務結果 | `task_id`, `status`, `message`, `options?` |
| `progress` | 進度報告 | `task_id`, `chat_id`, `message` |

注意：`task` 訊息中新增 `chat_id` 欄位，讓 agent 可以在 `progress` 中帶回，Hub 才知道推給哪個 Gateway/chat。

#### 2.3 Heartbeat

HTTP heartbeat loop 移除，改為 WS 層級 ping/pong。aiohttp WebSocket 支援 `heartbeat` 參數（例如 20 秒），自動偵測斷線。

Registry 的 `_is_alive` 改為檢查 WS 連線是否存在且未關閉。

#### 2.4 WS 斷開處理

**Agent WS 斷開：**
- Hub 標記 agent 為 offline
- 該 agent 所有 `working`/`waiting_input` 的 task 標記為 error
- 透過 Gateway WS 通知相關用戶：「Agent 已離線，任務已中斷」

**Gateway WS 斷開：**
- Hub 記錄斷線
- Gateway 自動重連（exponential backoff）

#### 2.5 Task 終止（Hub 後台）

Hub 後台「終止 task」按鈕 → Hub 透過 WS 送 `{"type": "cancel", "task_id": "xxx"}` 給對應 agent → Agent 停止該 task，回報 `{"type": "result", "task_id": "xxx", "status": "cancelled"}`。

Agent 端實作：batch 的 `run_batch` 迴圈在每則訊息處理之間檢查 cancel flag。收到 cancel 時設定 flag，下一次迴圈檢查到就停止。

#### 2.6 Hub 後台新增

- Agent 列表：WS 連線狀態（connected/disconnected）取代 heartbeat-based 判斷
- Gateway 區塊：顯示目前連線的 Gateway 數量、每個 Gateway 的配置（mode、phone/bot_token masked、allowed_chats）

Gateway 建立 WS 時上報配置：
```json
{"type": "register", "mode": "userbot", "phone": "+886***908", "allowed_chats": [-100xxx]}
```

#### 2.7 移除的東西

- Agent 的 HTTP `/task` endpoint（task 改走 WS）
- Agent 的 HTTP heartbeat loop
- Hub 的 HTTP `POST /heartbeat` endpoint
- Hub 的 `send_task_to_agent` HTTP client（`hub/cli.py` 中的函式）
- Gateway 的 HTTP `_dispatch_to_hub` 和 `_notify_message_id`

#### 2.8 保留的 HTTP endpoint

- `POST /register`：Agent 一次性註冊
- `POST /register_error`：Agent 初始化錯誤上報
- `GET /agents`：Dashboard 查詢
- 所有 Dashboard 相關 endpoint（`/`, `/dashboard/*`）
- 所有 Auth endpoint（`/auth/*`）
- Agent 的 `GET /health` 和 `GET /dashboard`

---

### 三、tg-transfer 與 WS 整合

#### 3.1 Batch 不再阻塞 HTTP

`_start_batch` 和 `_resume_batch` 在確認後立即回傳 `{"type": "result", "status": "done", "message": "開始搬移..."}`，然後在 event loop 中繼續執行 batch。

執行過程透過 WS 送 `progress` 訊息回報進度。完成/暫停/失敗時送 `result`。

#### 3.2 report_fn 改為 WS 推送

現有的 `report_fn` callback（目前是 `pass`）改為透過 WS 送 `progress` 訊息：

```python
async def report_fn(text):
    await self.ws_send({
        "type": "progress",
        "task_id": task_id,
        "chat_id": chat_id,
        "message": text,
    })
```

#### 3.3 Cancel 支援

`TransferEngine.run_batch` 增加 cancel 檢查：

```python
while True:
    if self._cancelled.get(job_id):
        await self.db.update_job_status(job_id, "cancelled")
        return "cancelled"
    msg_row = await self.db.get_next_pending(job_id)
    ...
```

Agent 的 WS message handler 收到 `cancel` 時設定 flag。

---

## 實作順序

1. **tg-transfer 影片修復**（1.1 ~ 1.4）
2. **Hub WS endpoint**（2.1 ~ 2.4）
3. **BaseAgent WS client**（2.3, 2.7）
4. **Gateway WS client**（2.4, 2.7）
5. **Hub 後台更新**（2.5, 2.6）
6. **tg-transfer WS 整合**（3.1 ~ 3.3）

## 受影響的檔案

| 檔案 | 改動 |
|------|------|
| `agents/tg_transfer/transfer_engine.py` | 影片 metadata、並行下載、album 原子性、cancel 檢查 |
| `agents/tg_transfer/hasher.py` 或新增 `media_utils.py` | ffprobe metadata helper |
| `agents/tg_transfer/__main__.py` | WS 整合、batch 非阻塞、report_fn |
| `core/base_agent.py` | WS 連線管理、移除 heartbeat、WS message handler |
| `core/models.py` | TaskRequest 新增 chat_id 欄位 |
| `hub/server.py` | 新增 WS endpoint、移除 /heartbeat |
| `hub/cli.py` | 移除 send_task_to_agent HTTP client |
| `hub/registry.py` | WS 連線狀態取代 heartbeat timestamp |
| `hub/dashboard.py` | 顯示 WS 狀態、Gateway 連線資訊 |
| `gateway/telegram_user_handler.py` | WS 取代 HTTP dispatch |
| `gateway/telegram_handler.py` | WS 取代 HTTP dispatch（bot mode） |
