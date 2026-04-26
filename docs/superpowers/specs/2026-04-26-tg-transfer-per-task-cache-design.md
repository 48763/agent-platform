# TG Transfer 下載緩存改 per-task-id 化設計

日期：2026-04-26
狀態：已實作（commits 7da2360..bdbb28c）

## 背景

目前 tg_transfer agent 的下載緩存 `data/tg_transfer/tmp/` 是一個扁平目錄，所有 task 共用，檔名為 `{msg_id}_{uuid8}.{ext}`。實際運作下會出現以下問題：

- 使用者從 hub dashboard 刪除對話後，agent 端的 cache 檔案（特別是 partial_path 斷點檔）沒有任何機制清掉
- transfer 中途異常退出（崩潰、被 kill）會在共用目錄留下孤兒檔，不易辨識歸屬
- 沒有「整批清掉一個 task 所有檔案」的單一動作

需求是把下載緩存按 task_id 分目錄存放，讓 task 生命週期跟 cache 目錄一一對應，刪 task 時可以整目錄 rmtree。

## 名詞對焦

| 名詞 | 在哪裡 | 是什麼 | ID 是否會變 |
|---|---|---|---|
| **task** | hub | 一段對話（hub `tasks` 表 PK，由 `task_manager.create_task` 用 uuid4 產生） | 從建立到 close/delete 不變 |
| **job** | agent | 一個 batch 搬移工作（agent `jobs` 表 PK，由 `db.create_job` 用 uuid4 產生） | 不變 |

預設關係：每個 hub task 對應 0 或 1 個 agent job（batch 模式才會建 job；單筆 transfer 不建）。

## 關鍵不變式

1. **task_id 不變**：hub 端 `task_id` 是 immutable PK，從建立到 close/delete 不變
2. **跨 task 接續 batch 不會發生**：`_resume_batch` 只能從 `_handle_paused_response` 觸發，前提是 `task.task_id in self._pending_jobs`；`_pending_jobs` 用 task_id 當 key，使用者另開新對話會拿到新 task_id 但找不到 `_pending_jobs` entry，無法接續舊 paused job
3. 因此 **一個 task_id 從生到死對應同一個 cache 目錄**，沒有目錄搬遷情境

## 設計

### 1. 目錄結構

```
data/tg_transfer/tmp/
├── {task_id_A}/
│   ├── {msg_id}_{uuid8}.mp4
│   ├── {msg_id}_{uuid8}.thumb.jpg
│   └── ...
└── {task_id_B}/
    └── ...
```

- task_id 從 `job.task_id` 取得（jobs 表已有此欄位）
- 單筆 transfer（非 batch）也走此規則：從 TaskRequest 拿 task_id

### 2. TransferEngine 改動

`TransferEngine.__init__(tmp_dir=...)` 維持收基礎目錄，但 transfer 方法簽章新增 `task_id` 參數：

- `transfer_album(..., task_id: str)` —— 開頭 `task_dir = os.path.join(self.tmp_dir, task_id); os.makedirs(task_dir, exist_ok=True)`，後續所有 `dest = os.path.join(task_dir, ...)`
- `transfer_single(..., task_id: str)` —— 同上
- `_download_with_resume(..., task_id)` —— partial_path 寫入時以 `task_dir` 為基準

外層呼叫 (`_run_batch_background`、`_handle_*` 直接 transfer) 都已有 task_id，傳進來即可。

### 3. 異常清理（核心需求）

#### 3a. Hub 端：刪除對話時通知 agent

`hub/dashboard.py::handle_task_delete` 在 DELETE `tasks` 表之前，多送一條 ws 訊息給綁定的 agent：

- 新增 `MsgType.TASK_DELETED`（在 `core/ws.py`）
- payload `{task_id}`
- 已 offline 的 agent 收不到 → agent 下次啟動時由 3c 的孤兒掃描兜底

#### 3b. Agent 端：收到 TASK_DELETED 處理

新增 `on_task_deleted(task_id: str)`：
1. `shutil.rmtree(os.path.join(self.engine.tmp_dir, task_id), ignore_errors=True)`
2. `await self.db.delete_jobs_by_task(task_id)` —— 新 helper，DELETE jobs + job_messages where task_id=?
3. 取消 `_bg_tasks[task_id]` 若仍 alive，清掉 `_pending_jobs/_current_chat_id/_search_state/_awaiting_target` 對應 entry

注意：不要等 agent 把當前正在跑的下載結束才刪——CANCEL 訊息已先發過（hub 端 `handle_task_delete` 對 working/waiting_input/waiting_approval 已會發 CANCEL），到 TASK_DELETED 時 bg task 應已停。仍存活就 cancel。

#### 3c. Agent 啟動孤兒掃描

`on_ws_connected` 後（`_resume_interrupted_jobs` 跑完之後）執行一次：

1. `os.listdir(self.engine.tmp_dir)` 拿到所有子目錄名
2. 用 `db.get_active_task_ids()` 取出仍有 active job 的 task_id 集合（status in pending/running/paused/awaiting_dedup）
3. 不在集合裡的目錄 → `shutil.rmtree`

這層用來補 ws 漏接（agent offline 時 hub 已 delete）。

### 4. 正常結束時的清理

維持現狀的 per-artefacts 刪除機制（transfer_album / transfer_single 末尾的 `for p in artefacts: os.remove(p)`），不改動。

**不主動 rmdir 空目錄**。理由：空目錄只佔一個 inode，沒實質成本；孤兒掃描（3c）下次啟動時必清。為了清空目錄在三個 background 協程加 finally 是過度工程。空目錄 = 待孤兒掃描清的下一輪。

### 5. 既有誤導註解清理

`agents/tg_transfer/__main__.py:1248-1251`：

```python
# If the reply came in under a new task_id (e.g. hub created a fresh
# task), rewrite the DB binding so future progress goes to the new task.
if job.get("task_id") != task_id or job.get("chat_id") != chat_id:
    await self.db.update_job_binding(job_id, task_id, chat_id)
```

`task_id != ` 比較永遠 false（_pending_jobs 不變式保證），改寫成只比較 `chat_id`，註解修正。

### 6. Migration 與向後相容

啟動時偵測 `tmp/` 下是否有任何「不是 task_id 子目錄」的檔案存在（舊扁平佈局的特徵）。若有：

1. 把這些檔案逐個 `os.remove`（這些檔對應舊 partial_path 已無法可靠歸屬到 task_id）
2. 對 `job_messages` 表執行 `UPDATE job_messages SET partial_path = NULL, downloaded_bytes = 0 WHERE partial_path IS NOT NULL`，強制下次從頭下載
3. 寫入旗標檔 `tmp/.migrated_v2` 防重複

下次啟動見到旗標檔則跳過 migration。

## 不在範圍內

- 線上重啟未執行下載任務的 bug（#2）—— 另案 systematic-debugging 處理，需要 production log
- 任何 dedup / phash / target index 的調整
- hub 端 task 自然到期 (done→archived→closed) 時是否觸發 cache 清理 —— 維持現狀（done 後 cache 應已自然清空，不額外處理）

## 測試計畫

1. **單元測試**：transfer_album / transfer_single 帶 task_id，驗證寫入路徑在 `{tmp}/{task_id}/`
2. **整合測試**：模擬 hub 發 TASK_DELETED → agent 端目錄消失 + DB 紀錄消失
3. **整合測試**：tmp 下放一個沒有 active job 的孤兒目錄，啟動 agent → 該目錄被清掉
4. **手動驗證**：實機跑一次 batch（10+ 訊息），中途 dashboard 刪 task，確認：
   - tmp/{task_id}/ 目錄消失
   - jobs / job_messages 該 task_id 紀錄消失
   - 沒有殘留 partial 檔
