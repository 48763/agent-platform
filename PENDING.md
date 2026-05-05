# 待處理工作

接手前先讀這份；解決後請從本檔移除對應條目。

## 1. tg-transfer-agent Telethon 殭屍化（未修）

**症狀**：用戶撞到 `執行失敗：Cannot send requests while disconnected`，所有任務都失敗，要等到 Telethon 偶然重連成功才恢復（觀察過撐 16 分鐘）。

**Root cause（兩層）**：

1. **時鐘漂移**：macOS Docker Desktop 在筆電睡眠/喚醒後 VM 時鐘會嚴重落後。曾在 log 看到 `System clock is wrong, set time offset to 4052s`（67 分鐘）。Telegram 拒收簽章不對的 MTProto 訊息 → 砍連線。
2. **缺自救**：`agents/tg_transfer/tg_client.py` 只在啟動時 `await client.start()` 一次，之後從不檢查連線。Telethon 內部 auto-reconnect 失敗 5 次後就放棄，agent 進入殭屍狀態。對比 gateway 用 `run_until_disconnected()`，斷線會 raise → 容器重啟自救；tg-transfer-agent 沒這層。

**止血**：`docker compose restart tg-transfer-agent`

**修復選項（擇一）**：

- **A（建議，最小改動）**：在 `tg_client.py` 或 `chat_resolver.py` 入口包一層 `try/except ConnectionError` → `await client.connect()` → 重試一次。修對結構性缺陷，不踢 in-flight 任務。
- **B（仿 gateway 風格）**：跑一個 watchdog coroutine 監看 `client.is_connected()`，斷了就 raise 讓容器重啟。風格一致但會踢掉所有 in-flight 任務。
- **C（治時鐘根源）**：容器 entrypoint 加 ntp sync，或 host wake 後自動重啟 Docker。治本但動到 host 環境。

**相關檔案**：
- `agents/tg_transfer/tg_client.py`（client 建立處）
- `agents/tg_transfer/chat_resolver.py`（最常觸發錯誤的呼叫路徑）
- `gateway/telegram_user_handler.py`（可參考的 reconnect 模式）

---

## 2. `resolve_chat` 沒 cache 導致 FloodWait（未修）

**症狀**：用戶撞到 `執行失敗：A wait of N seconds is required (caused by CheckChatInviteRequest)`（觀察過 N=118）。

**Root cause**：`agents/tg_transfer/chat_resolver.py` 第 27 行對每個 invite link 都呼叫 `CheckChatInviteRequest`，沒有 cache。同一個 batch 流程裡至少 2 次（`_handle_batch_request` → 用戶確認 → `_start_batch` 又對 source/target 各 resolve 一次），重啟後 `_resume_interrupted_jobs` 也會重 resolve；高頻使用就被 Telegram 限速。

**止血**：等 server 指定的秒數過去再操作。

**修復**：在 `chat_resolver` 模組或 client 物件上加一個 dict cache，key = invite_hash（或 normalized identifier），value = 解析過的 entity。失效策略可以最簡單：process lifetime 內一直留著（entity 物件本身會帶 access_hash，不需要重新驗證）。同時建議檢查 `_resume_interrupted_jobs` 等流程能不能改用 stored entity 而非 string identifier 二次 resolve。

**相關檔案**：
- `agents/tg_transfer/chat_resolver.py`（加 cache）
- `agents/tg_transfer/__main__.py`（呼叫點：489、490、766、860-861、990、1171、1352、1399-1400）
