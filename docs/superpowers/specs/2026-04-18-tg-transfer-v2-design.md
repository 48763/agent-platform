# TG Transfer Agent v2 — 媒體去重、搜尋、標籤、統計、存活檢查

## 概述

在現有 TG Transfer Agent 基礎上新增五項功能：
1. 媒體雜湊去重（SHA-256 精確 + pHash 感知相似）
2. 查詢功能（關鍵字搜文字 + 以圖搜圖，翻頁，目標群組連結）
3. 標籤系統（自動提取 #tag + 搬移時沿用保存）
4. 儲存統計 Dashboard（Agent 自建 endpoint）
5. 資源存活檢查（定期檢查目標訊息是否存在）

## 設計原則

- 新功能不破壞現有搬移流程，`jobs` 和 `job_messages` 表不動
- 媒體資產記錄獨立於搬移記錄（不同職責）
- pHash 為可選功能，缺少依賴時降級為 SHA-256 only

---

## 新增資料庫 Schema

### media 表 — 媒體資產記錄

```sql
CREATE TABLE media (
    media_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256          TEXT NOT NULL,
    phash           TEXT,                    -- 可為 NULL（依賴不可用或非圖片/影片）
    file_type       TEXT NOT NULL,           -- photo / video / document
    file_size       INTEGER,
    caption         TEXT,
    source_chat     TEXT NOT NULL,
    source_msg_id   INTEGER NOT NULL,
    target_chat     TEXT NOT NULL,
    target_msg_id   INTEGER,                -- 上傳成功後填入
    status          TEXT DEFAULT 'pending',  -- pending / uploaded / skipped
    job_id          TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_checked_at TIMESTAMP
);

CREATE UNIQUE INDEX idx_media_sha256_target ON media(sha256, target_chat);
CREATE INDEX idx_media_phash ON media(phash);
CREATE INDEX idx_media_caption ON media(caption);
CREATE INDEX idx_media_status ON media(status);
```

### tags 表 — 標籤

```sql
CREATE TABLE tags (
    tag_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE
);
```

### media_tags 表 — 多對多關聯

```sql
CREATE TABLE media_tags (
    media_id INTEGER NOT NULL,
    tag_id   INTEGER NOT NULL,
    PRIMARY KEY (media_id, tag_id),
    FOREIGN KEY (media_id) REFERENCES media(media_id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
);
```

### 狀態說明

| 狀態 | 說明 |
|------|------|
| `pending` | 已下載建檔，等待上傳 |
| `uploaded` | 上傳成功，`target_msg_id` 已填入 |
| `skipped` | 上傳失敗被跳過，記錄保留供下次重試 |

**刪除策略：**
- 存活檢查發現目標訊息不存在 → 直接刪除 `media` 記錄 + 關聯 `media_tags`
- 使用者主動刪除 → 同樣整筆刪除

---

## 業務邏輯

### 搬移時的媒體處理流程

```
下載媒體到暫存
  → 計算 SHA-256 + pHash
  → 提取 caption 中的 #tag
  → 查 media 表：sha256 + target_chat 是否有 uploaded 或 pending 記錄？
    → 有 uploaded → 跳過，不上傳
    → 有 pending → 正在搬移中，跳過
    → 無 → 查 pHash 是否有相似記錄（Hamming distance ≤ 10）？
      → 有相似 → 回傳相似項目（部分文字+超連結），詢問使用者是否仍要上傳
      → 無相似 → 寫入 media（status=pending）→ 上傳
        → 成功 → status=uploaded，填 target_msg_id，寫入 tags
        → 失敗被跳過 → status=skipped
        → 失敗被刪除 → 刪除 media 記錄
```

### pHash 計算流程

**圖片：**
```
原圖 → Pillow 開啟為 bitmap → 縮小 32x32 → 灰階化
  → DCT（離散餘弦變換）→ 取左上 8x8 區塊 → 計算平均值
  → 高於平均 = 1，低於平均 = 0 → 64-bit hash（16 字元 hex）
```

使用 `imagehash.phash(Image.open(path))` 實作。

**影片：**
```
ffmpeg 抽取第 1 秒的幀 → 輸出為圖片 → 走圖片 pHash 流程
```

**不支援時的降級：**
- 缺少 Pillow/imagehash → phash 欄位存 NULL
- 缺少 ffmpeg → 影片 phash 存 NULL
- 去重仍靠 SHA-256 正常運作
- 以圖搜圖功能 → 回傳「pHash 不可用，僅支援關鍵字搜尋」

**Hamming distance 計算：**
```python
def hamming_distance(hash1: str, hash2: str) -> int:
    return bin(int(hash1, 16) ^ int(hash2, 16)).count('1')
```

相似閾值：≤ 10 視為相似。

**效能：** SQLite 無原生 Hamming distance，全量撈出在 Python 端計算。資料量幾萬筆以內無效能問題。

### 標籤處理

- 搬移時自動從 caption 提取 `#xxx` 作為 tag（正則：`#(\w+)`）
- 原始訊息的 tag 沿用保存
- tags 表去重（同名 tag 只存一筆，`INSERT OR IGNORE`）
- 一個媒體可有多個 tag，透過 media_tags 多對多關聯

---

## 查詢功能

### 關鍵字搜尋

使用者發送關鍵字 → 搜尋 `media.caption LIKE '%關鍵字%'` + `tags.name` 匹配 → 合併去重 → 按 `created_at DESC` 排序。

### 以圖搜圖

使用者貼圖片 → 下載 → 計算 pHash → 撈出所有有 phash 的 uploaded 記錄 → 計算 Hamming distance ≤ 10 的結果。

### 結果格式

每頁 10 筆，每筆：caption 前 50 字作為預覽文字，做成超連結指向目標群組訊息。

範例：
```
1. 這是一段影片的說明文字，包含了一些教學內容...
   https://t.me/c/123456/789
2. 另一段照片的描述文字...
   https://t.me/c/123456/790
```

翻頁透過 Hub 的多輪對話機制，回傳「下一頁 / 上一頁」選項。

---

## 儲存統計 Dashboard

Agent 在 `/dashboard` endpoint 提供 HTML 頁面，顯示：

- **總儲存媒體數量** — `SELECT COUNT(*) FROM media WHERE status='uploaded'`
- **標籤總數** — `SELECT COUNT(*) FROM tags`
- **各標籤對應數量** — `SELECT t.name, COUNT(*) FROM tags t JOIN media_tags mt ... GROUP BY t.name ORDER BY COUNT(*) DESC`

純 SQL 聚合查詢，不需額外計數表。Hub 暫不代理，之後再做。

---

## 資源存活檢查

**觸發方式：** Agent 內建背景 asyncio task，定時執行。

**檢查間隔：** 可透過 `agent.yaml` 的 `settings.liveness_check_interval` 設定，預設 24 小時（單位：小時）。

**流程：**
```
每 N 小時掃描 status='uploaded' 且 last_checked_at < N 小時前 的記錄
  → 批次 50 筆，避免 Telegram rate limit
  → 用 Telethon get_messages 檢查 target_chat + target_msg_id 是否存在
  → 存在 → 更新 last_checked_at
  → 不存在 → 刪除 media 記錄 + 關聯 media_tags
```

---

## agent.yaml 設定新增

```yaml
settings:
  # ...existing settings...
  liveness_check_interval: 24   # 小時，存活檢查間隔
  search_page_size: 10          # 查詢結果每頁筆數
  phash_threshold: 10           # pHash Hamming distance 相似閾值
```

---

## 新增依賴

- `imagehash` — pHash 計算
- `Pillow` — 圖片處理
- `ffmpeg`（Dockerfile apk 安裝）— 影片抽幀

---

## 新增/修改檔案

```
agents/tg_transfer/
├── media_db.py          # 新增：media/tags/media_tags 表操作
├── hasher.py            # 新增：SHA-256 + pHash 計算、Hamming distance
├── search.py            # 新增：關鍵字搜尋、以圖搜圖、翻頁
├── tag_extractor.py     # 新增：從 caption 提取 #tag
├── liveness_checker.py  # 新增：背景存活檢查 task
├── dashboard.py         # 新增：統計 HTML endpoint
├── transfer_engine.py   # 修改：搬移流程中加入 hash 計算、去重、tag 處理
├── __main__.py          # 修改：加入查詢 dispatch、啟動存活檢查、註冊 dashboard
├── agent.yaml           # 修改：新增設定項
├── Dockerfile           # 修改：加裝 Pillow、imagehash、ffmpeg
```
