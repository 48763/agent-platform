# TG Transfer Agent 優化（13 項）設計

日期：2026-04-29
狀態：已實作（commits 94ba6a3..e4359df）

## 背景

audit 結果整理出 13 個優化項目，跨效能 / 程式品質 / 維護性 / 邊角穩定性。每項都有具體 file:line 跟修法。

設計目標：

- **效能**：拿掉 hot-path 上的浪費（重複 ffprobe、event loop 阻塞、N+1 query、整表掃）
- **正確性**：消除潛藏的 race / leak（liveness task 沒人接手異常、雙連線寫鎖打架）
- **可維護**：把 1497 行的 `__main__.py` 拆成可推理的單元

## 13 項目分組

按檔案/範疇分 6 組，每組獨立可 ship、互不依賴：

### Group A — DB schema + 寫入效能

- **A1（#4）** `db.add_messages` 用 `executemany` 取代 N 次 await execute
- **A2（#5）** 兩個 aiosqlite 連線都啟用 `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL`
- **A3（#1b）** 新增 `idx_media_phash_lookup ON media(target_chat, file_type, status) WHERE phash IS NOT NULL`
- **A4（#12）** 新增 `idx_media_target_msg ON media(target_chat, target_msg_id)`

### Group B — Engine 阻塞解除 + ffprobe 共用

- **B1（#2）** `compute_sha256` / `compute_phash` / `compute_phash_video` 在 transfer 路徑用 `asyncio.to_thread` 包起來
- **B2（#3）** `_transfer_media` 跟 `transfer_album` 內 video 路徑只跑一次 `ffprobe_metadata`，結果共用給 dedup gate 跟 upload attributes

### Group C — 快取 hot-path 重複查詢

- **C1（#1a）** `run_batch` 維護 `(file_type, target_chat) → phash candidates` 的 batch-scoped 快取，`_transfer_media` 透過參數接收，避免每筆訊息都查整表
- **C2（#11）** `_size_limit_bytes` 加 5 秒 TTL 快取（live-change 還是會生效，只是延遲 5 秒）

### Group D — DB helpers

- **D1（#6）** `MediaDB.search_keyword` 改用 `COUNT(*)` 一查 + `LIMIT/OFFSET` 一查兩段式，不再把全部結果撈進記憶體切片
- **D2（#8）** 新增 `TransferDB.batch_mark_failed_as_skipped(job_id)` 用一次 `UPDATE ... WHERE status='failed'`，移除 `__main__.py:850` 直接戳 `self.db._db` 的封裝破壞

### Group E — `__main__.py` 雜項

- **E1（#9）** 在 `_handle_batch_request` 撈完訊息後快取到 `self._batch_message_cache: dict[job_id, list[Message]]`，`_start_batch` 直接消費；消費後 evict
- **E2（#10）** `run_liveness_loop` 的 task 存在 `self._liveness_task`，加 `add_done_callback` 偵測異常退出時 log + 重新排程
- **E3（#13）** Album 偵測改用 `iter_messages(chat, ids=msg.grouped_id, ...)` 走 grouped_id；Telethon API 不支援的話 fallback 到原 ±10 ID 範圍但暴露成 `agent.yaml` setting `album_window` 預設 10

### Group F — `__main__.py` 結構重構（最大）

- **F1（#7）** 抽 `BatchController` 類別到 `agents/tg_transfer/batch_controller.py`，搬：
  - `_run_batch_background`
  - `_run_defer_scan_background`
  - `_run_process_deferred_background`
  - 對應的 `_start_batch` / `_start_defer_scan` / `_handle_process_deferred`
  - 共用的 error-boundary wrapper（unhandled exception → 透過 ws 回報 ERROR + 終止 job）
  - 把 `self._bg_tasks` 移到 controller 內，agent 透過 controller proxy 取得 task

## API 變更總覽

### `db.py`

- **修改** `add_messages` 用 `executemany`
- **新增** `batch_mark_failed_as_skipped(job_id) -> int`
- **修改** `init()` 啟用 WAL pragma

### `media_db.py`

- **修改** `init()` 啟用 WAL pragma
- **修改** `search_keyword` 改兩段查詢
- **新增** index `idx_media_phash_lookup`、`idx_media_target_msg`

### `transfer_engine.py`

- **修改** `_transfer_media` 接收 `phash_candidates` 參數，跳過內部 fetch
- **修改** `transfer_album` 同上
- **修改** `run_batch` 維護 phash candidates 快取
- **修改** `_size_limit_bytes` 加 5s TTL 快取
- **修改** video 路徑 ffprobe 共用結果
- **修改** hash 計算路徑用 `asyncio.to_thread`

### `hasher.py`

- **新增** async wrapper helpers（或保持 sync 由 caller 包 to_thread）

### `liveness_checker.py`

- 不動（剛重寫過）

### `__main__.py`

- **拆出** `BatchController` 類別到新檔案 `batch_controller.py`
- `__main__.py` 行數預期降到 ~1000 以下
- **修改** `_handle_batch_request` / `_start_batch` 加訊息快取
- **修改** liveness task 啟動加 done_callback

### `agent.yaml`

- **新增** `album_window: 10` setting

## 不在範圍內

- liveness loop 設計（剛重寫）
- 跨 batch dedup 改 media 表（剛實作）
- 任何新功能（純優化）
- search_keyword 用 FTS5（重大改動，未來再考慮）

## 風險評估

| 項目 | 風險 | 緩解 |
|---|---|---|
| WAL 啟用（A2） | SQLite 行為改變，多 reader 在 WAL 期間不卡寫但有額外 -wal/-shm 檔 | 啟動時新檔案會自動建立，本機 docker volume 已 mount tmpfs 安全 |
| BatchController 抽出（F1） | 大 refactor 影響 hub 端 progress/result 路由 | TDD 嚴格、保留所有現有 ws message 行為，整體 regression 測試 |
| 快取（C1, C2） | 跨 batch 看到舊資料 | C1 是 batch-scoped 不會跨 batch；C2 5s TTL 對 live config 改動延遲是 acceptable |
| Album window (E3) | Telethon API 變動風險 | 加 fallback + 開 setting |

## 測試計畫

- 既有 4xx 個 unit/integration test 全綠（含 2 個 pre-existing test_integration.py 失敗繼續忽略）
- 每個 task 加新 test case 覆蓋變動行為
- F1 重構需要 `tests/test_tg_transfer_integration.py` 做 batch lifecycle 端到端測試
- 部署後跑一個小 batch 確認 transfer 正常、metric 沒有變糟

## 實作順序

依風險遞增、依賴順序：

1. **A**（純 DB 層、零行為改動，用作基礎驗證）
2. **B**（透過 `to_thread` 拆掉阻塞，先觀察 event loop 是否真有改善）
3. **C**（依賴 B 已穩，加 batch 快取）
4. **D**（純 helper 新增/重構，獨立）
5. **E**（小範圍 `__main__.py` 雜項）
6. **F**（最大重構，最後做，前面建立的 confidence 都用得上）
7. **deploy**（Task 5 deployment gate）
