# TG Transfer 重複搬移修復 + Liveness Loop 重寫設計

日期：2026-04-28
狀態：待實作

## 背景與動機

兩件耦合在一起的事：

**1. 重複搬移會再下載（bug）**
完成 batch 後重跑同樣 source→target，agent 會再下載每一筆的 thumb 走 Phase 4 dedup，浪費 bandwidth、且 caption/file_size 任一不同會掉到 ambiguous 佇列。根因：`db.get_transferred_message_ids` 查的是 `job_messages`，但 job 走到 terminal status 時這張表會被 DELETE，所以「已搬過」紀錄消失。

**2. Liveness loop 機制過時**
現況 `run_liveness_loop` 每 24h 撈 50 筆 stale media 檢查存在性，但：
- `limit=50` 是寫死的、且被當總配額（不是 batch size）→ 110 筆要 3 天才掃完
- `sleep(24h)` 跟掃描進度無關 → 無法保證「全掃完一輪」
- 沒有 scan run 紀錄、進度全靠 `last_checked_at` 隱性推斷
- 沒有「掃到一半重啟」的續跑機制
- 不偵測訊息**內容變動**（caption/tag），DB 永遠停留在第一次搬時的快照

兩件事一起修因為：dedup 倚賴 `media` 表（`status='uploaded'` row），liveness loop 是維護 `media` 表新鮮度的機制。它們是一體兩面。

## 目標

- **A**：完成過的 batch 重跑時，已搬訊息**完全不下載**（連 thumb 都不下）
- **B**：Liveness loop 確保 `media` 表反映 target chat 的真實狀態
  - 不存在的訊息 → 刪 row
  - caption 變動 → 更新 `media.caption` + 重抽 tags
  - 不重算 phash / thumb_phash（成本太高，先不做）

## 名詞對焦

| 名詞 | 意義 |
|---|---|
| **Liveness scan** | 一輪「掃過所有 uploaded media、確認每筆狀態」的工作 |
| **Plan file** | `.liveness/<uuid>.json`，紀錄該 scan 還沒處理的 media_id 列表 |
| **stale**（廢棄詞） | 不再使用「stale」概念；改為「全掃」 |
| **`last_updated_at`** | media row 的 metadata 最近被偵測到變動的時間（不是被掃過的時間） |

## 設計

### A. 修 dedup 查詢

`db.get_transferred_message_ids(source_chat, target_chat)` 改查 `media` 表：

```sql
SELECT source_msg_id FROM media
WHERE source_chat = ? AND target_chat = ? AND status = 'uploaded'
```

**行為改變：**
- 完成過的 batch（job_messages 已 wipe）也能查到
- 任何 `status='uploaded'` 的 media 都算「已搬過」
- 一次相簿 = 10 row，10 個 source_msg_id 都會被回傳，相簿天然支援

**Caller 不變**：`__main__.py:771` 跟 `:891` 兩處呼叫照舊。

### B. Liveness Loop 重寫

#### B.1 目錄結構

```
data/tg_transfer/tmp/
├── {task_id}/                  # 下載 cache（既存）
└── .liveness/                  # 新增：scan 計畫檔
    └── <uuid>.json             # 進行中 scan 的剩餘 media_id 列表
```

`.liveness/` 是 dotfile，既有的 orphan scan / legacy migration 都會自動略過，無需特別處理。

#### B.2 Plan file 格式

`.liveness/<uuid>.json`：

```json
{
  "scan_id": "8a7f3c2e-...",
  "started_at": "2026-04-28T10:00:00Z",
  "remaining": [123, 456, 789, ...]
}
```

只存還沒處理的 media_id（已處理的從 `remaining` 移除）。

#### B.3 Scan 主迴圈

```
async def run_liveness_loop():
    while True:
        await run_one_scan()
        await asyncio.sleep(24 * 3600)   # 固定 24h，不管掃多久

async def run_one_scan():
    plan_path = locate_or_create_plan()
    while True:
        plan = load(plan_path)
        if not plan["remaining"]:
            os.remove(plan_path)
            logger.info(f"Liveness scan {plan['scan_id']} done")
            return
        batch = plan["remaining"][:50]
        await process_batch(batch)
        plan["remaining"] = plan["remaining"][50:]
        atomic_rewrite(plan_path, plan)

def locate_or_create_plan():
    existing = glob(".liveness/*.json")
    if existing:
        return existing[0]   # 重啟續跑
    plan = {
        "scan_id": uuid4(),
        "started_at": now(),
        "remaining": media_db.list_all_uploaded_ids(),
    }
    write(f".liveness/{plan['scan_id']}.json", plan)
    return path
```

#### B.4 一次只一個 scan

由 plan file 存在性自動保證：

- 啟動時若 `.liveness/*.json` 存在 → 接續舊 plan，不開新的
- 完成才刪 plan file → sleep 24h → 下輪建新 plan

不需要鎖、不需要狀態機。

#### B.5 重啟續跑

`locate_or_create_plan()` 偵測殘留檔即接續。`pop 50 → process → rewrite` 是冪等的：

- 處理到一半 crash → 重啟後從 plan file 殘餘列表繼續
- 已處理但還沒 rewrite 的 50 筆 → 會被重跑（API call 是冪等的，無副作用）
- atomic rename（`<uuid>.json.tmp` → `mv`）防止半寫狀態

#### B.6 處理單筆邏輯

```
process_one(media_id):
    row = media_db.get_media(media_id)
    if not row:
        return  # row 已被別處刪掉，跳過
    msg = await client.get_messages(target_entity, ids=row["target_msg_id"])
    if msg is None:
        await media_db.delete_media(media_id)  # 訊息消失
        return
    new_caption = msg.text or msg.message or ""
    if new_caption != row["caption"]:
        await media_db.update_caption_and_tags(media_id, new_caption)
        # update_caption_and_tags 內部：
        #   UPDATE media SET caption=?, last_updated_at=CURRENT_TIMESTAMP
        #   DELETE FROM media_tags WHERE media_id=?
        #   re-extract tags + INSERT INTO media_tags
```

**不偵測**：phash / thumb_phash / file_size / duration。

### C. Schema Migration

#### C.1 變更

```sql
ALTER TABLE media ADD COLUMN last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
UPDATE media SET last_updated_at = COALESCE(last_checked_at, created_at);
ALTER TABLE media DROP COLUMN last_checked_at;
```

#### C.2 SQLite 版本相依性

`ALTER TABLE DROP COLUMN` 需要 SQLite 3.35+（2021 年 3 月發布）。Migration 前需先檢查 SQLite 版本，舊版退路：

```python
sqlite_version = tuple(int(x) for x in sqlite3.sqlite_version.split('.'))
if sqlite_version >= (3, 35, 0):
    "ALTER TABLE media DROP COLUMN last_checked_at"
else:
    logger.warning("SQLite < 3.35, leaving last_checked_at column in place")
```

舊版 SQLite 的場景下欄位留著，code 不再讀寫，無功能影響。

#### C.3 Migration 旗標

放在 `media_db.py::_migrate()`（既有 method，已用 `PRAGMA table_info` 檢查欄位）裡，加進現有的條件分支：

```python
async with self._db.execute("PRAGMA table_info(media)") as cur:
    cols = {row["name"] for row in await cur.fetchall()}

if "last_updated_at" not in cols:
    await self._db.execute(
        "ALTER TABLE media ADD COLUMN last_updated_at TIMESTAMP"
    )
    # 從現有時間欄位複製初值，避免新欄位是 NULL 看起來像「從未掃過」
    await self._db.execute(
        "UPDATE media SET last_updated_at = COALESCE(last_checked_at, created_at)"
    )

if "last_checked_at" in cols:
    sqlite_version = tuple(int(x) for x in sqlite3.sqlite_version.split('.'))
    if sqlite_version >= (3, 35, 0):
        await self._db.execute("ALTER TABLE media DROP COLUMN last_checked_at")
    else:
        logger.warning(
            "SQLite %s < 3.35.0, leaving dead last_checked_at column. "
            "Code no longer reads/writes it; safe to ignore.",
            sqlite3.sqlite_version,
        )
```

冪等：跑過一次後 `last_updated_at` 已存在、`last_checked_at` 已不存在，下次啟動兩個分支都跳過。

### D. Dedup 命中不更新 `last_updated_at`

**設計決策**：dedup 命中只代表「我們認為這筆已存在」，沒有實際對 target 拉新內容、沒有偵測 metadata 變動。`last_updated_at` 嚴格只反映「scan 確認 caption/tag 跟現實一致的時間」。Dedup path 不寫這欄。

## API 變更總覽

### `media_db.py`

**移除：**
- `update_last_checked(media_id)`
- `get_stale_media(max_age_hours, limit)`

**新增：**
- `list_all_uploaded_ids() -> list[int]` — 給 scan plan 用
- `update_caption_and_tags(media_id, caption: str)` — 偵測到 caption 變動時呼叫，UPDATE caption + bump last_updated_at + DELETE old tags + INSERT new tags

**Schema：**
- ADD `last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`
- DROP `last_checked_at`（SQLite 3.35+）

### `db.py`

**修改：**
- `get_transferred_message_ids` 查 `media` 表而非 `job_messages`

### `liveness_checker.py`

**重寫：**
- 移除舊 `check_batch` / `run_liveness_loop` 流程
- 新增 plan file load/save helpers
- 新增 `run_one_scan()` 單輪掃描
- 新增 `process_one(media_id)` 單筆處理
- `run_liveness_loop()` 變成 `while True: run_one_scan(); sleep(24h)`

### `__main__.py`

無改動（`run_liveness_loop` 入口不變）。

## 不在範圍內

- **相簿主訊息優化**（只比對相簿第一則的 caption）：等部署後觀察 API call 量再評估
- **freshness mode / 重抽 phash**：頻寬成本太高
- **dashboard 顯示 scan 進度**：未來想加可以用 plan file 推
- **手動 `/recheck_target` 指令**：未來功能，本輪不做

## 測試計畫

### Unit tests

- `media_db.list_all_uploaded_ids` 只回 status='uploaded' 的 row
- `media_db.update_caption_and_tags`：
  - caption 寫入
  - `last_updated_at` 被 bump
  - 舊 tags 清除、新 tags 寫入
- `db.get_transferred_message_ids` 查 `media` 表，多種 status 過濾

### Integration tests（liveness）

- Plan file lifecycle：開始、消化、完成、刪除
- Pop batch + atomic rewrite：crash 後 remaining 仍正確
- 重啟接續：殘留 plan file 被使用，不開新的
- caption diff → metadata 與 tags 都更新
- 訊息消失 → media row 被刪
- 不存在的 media_id（已被別處刪）→ 安全跳過

### Migration tests

- ADD `last_updated_at` 後欄位存在、值從 created_at 複製
- DROP `last_checked_at`（在支援的 SQLite 版本）
- Migration 冪等

### Manual e2e

- 真實 batch → completed → 重跑同樣 batch → 確認 0 下載
- 手動編輯 target 訊息 caption → 等 liveness 觸發 → 確認 DB 同步
- 手動刪 target 訊息 → 等 liveness → 確認 row 被刪
- agent 重啟中途 → 殘留 plan file → 接續處理
