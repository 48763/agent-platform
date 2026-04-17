# TG Transfer Agent 設計規格

## 概述

Telegram 群組間資源搬移工具，以獨立 agent 形式運行於現有多 agent 平台上。支援單則搬移與批量搬移，具備斷點續傳與跨 Job 去重能力。

## 設計原則

- AI 僅用於任務釐清（自然語言 → 結構化指令）與結果回報，搬移邏輯純程式執行
- Agent 自帶獨立 Telethon client，不依賴 Gateway
- 流式處理（逐則下載上傳），搭配 SQLite 記錄實現斷點續傳

---

## 架構

```
User (via TG) → Hub → TG Transfer Agent
                          ├─ AI Layer (Gemini Flash) — 釐清任務、解析指令、回報結果
                          ├─ Chat Resolver — 群組名稱/邀請連結 → chat entity
                          ├─ Transfer Engine — 下載/上傳邏輯
                          ├─ Telethon Client — 獨立 TG 連線
                          └─ SQLite DB — job/訊息搬移狀態/使用者設定
```

---

## 觸發方式

| 觸發方式 | 行為 | 需確認 |
|---------|------|--------|
| 貼 TG 訊息連結 | 解析連結，下載原檔，上傳到目標 | 否 |
| 轉發訊息進來 | 偵測轉發來源，下載原檔，上傳到目標 | 否 |
| 批量指令（自然語言） | AI 解析來源群組+篩選條件，回報預估數量，等確認後執行 | 是 |

---

## 來源與目標辨識

**來源：**
- 單則/少量 — TG 訊息連結（如 `https://t.me/c/123456/789`）
- 批量 — 群組名稱（`@channel_name`）或邀請連結（`https://t.me/+AbCdEfG`）

**目標：**
- 指令中明確指定，或使用預設 `default_target_chat`
- 預設值可透過 `agent.yaml` 設定，也可透過 bot 動態修改（持久化到 SQLite config 表）

---

## Transfer Engine

### 單則搬移流程

```
收到訊息連結或轉發 → 解析 chat_id + message_id
  → 檢查是否為 media group（album）
    → 是：抓取同組所有訊息（同 grouped_id）
    → 否：單則處理
  → 下載媒體到暫存 → 上傳到 target_chat（帶原始 caption）→ 清除暫存 → 回報完成
```

### 批量搬移流程

```
AI 解析來源群組 + 篩選條件 → 計算符合條件的訊息數量
  → 回報預估數量，等使用者確認
  → 建立 Job，逐則寫入 DB（status=pending）
  → 搬移迴圈：
      取下一則 pending 訊息 → 下載 → 上傳 → 標記 success → 清除暫存
      每 20 則回報一次進度
      失敗 → 重試最多 3 次 → 仍失敗 → 暫停 Job，提供選項：
        - 重試：再試一次
        - 跳過：跳過這則繼續
        - 一律跳過：後續失敗都自動跳過
  → 全部完成 → 回報結果摘要
```

### 支援的訊息類型

| 類型 | 處理方式 |
|------|---------|
| 純文字 | `send_message` |
| 圖片 | 下載 → `send_file` |
| 影片 | 下載 → `send_file` |
| 檔案 | 下載 → `send_file` |
| 語音 | 跳過（skip） |
| 貼圖 | 跳過（skip） |
| 投票 | 跳過（skip） |

### Media Group（Album）處理

- 同一 `grouped_id` 的訊息視為一組
- 遇到 album 中的第一則時，一次抓取同組所有訊息
- 用 `send_file` 以 album 方式一次上傳，保持原始分組
- 成功/失敗以組為單位

### 暫存策略

- 下載到 `/tmp/tg_transfer/{job_id}/`
- 單則完成上傳後立即刪除
- Job 完成或失敗後清理整個目錄

---

## AI Layer

**介入時機：**
1. **任務解析** — 自然語言 → 結構化 Job 參數（JSON）
2. **結果回報** — 搬移結果 → 使用者易讀訊息

**不經過 AI：**
- 貼連結 → 正則解析，直接執行
- 轉發訊息 → 偵測 forward 屬性，直接執行
- 進度回報 → 固定模板字串格式化

**AI 選擇：** Gemini Flash（輕量意圖解析，快且便宜）

---

## SQLite Schema

```sql
CREATE TABLE jobs (
    job_id      TEXT PRIMARY KEY,
    source_chat TEXT NOT NULL,
    target_chat TEXT NOT NULL,
    filter_type TEXT,              -- date_range / count / all
    filter_value TEXT,             -- JSON: {"from":"...","to":"..."} 或 {"count":50}
    mode        TEXT NOT NULL,     -- single / batch
    status      TEXT DEFAULT 'pending',  -- pending / running / paused / completed / failed
    auto_skip   BOOLEAN DEFAULT FALSE,
    total       INTEGER DEFAULT 0,
    success     INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE job_messages (
    job_id      TEXT NOT NULL,
    message_id  INTEGER NOT NULL,
    grouped_id  INTEGER,
    status      TEXT DEFAULT 'pending',  -- pending / success / failed / skipped
    retry_count INTEGER DEFAULT 0,
    error       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (job_id, message_id),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX idx_transfer_history ON job_messages(job_id, message_id, status);

CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

**資料保留策略：** 永久保留，不自動清除。記錄用於跨 Job 去重。

### 斷點續傳

1. Job 建立時，遍歷來源訊息，全部寫入 `job_messages`（status=pending）
2. 搬移迴圈每次查 `WHERE job_id=? AND status='pending' ORDER BY message_id ASC`
3. 成功 → status=success；失敗重試超限 → status=failed，Job 暫停（或 auto_skip 時標記 skipped 繼續）
4. 使用者選擇繼續 → Job 恢復 running，從下一則 pending 繼續
5. 容器重啟 → 查詢 `status='running'` 的 Job，自動恢復

### 跨 Job 去重

對同一對 `source_chat → target_chat` 建新 Job 時，比對歷史 `job_messages` 中已 success 的 `message_id`，新 Job 中這些訊息直接標記 skipped。

---

## 檔案結構

```
agents/tg_transfer/
├── agent.yaml          # 路由、優先級、設定
├── __main__.py         # Agent 入口，handle_task 分派
├── transfer_engine.py  # 下載/上傳/album/進度回報
├── chat_resolver.py    # 名稱/連結 → entity 解析
├── db.py               # SQLite 管理
├── tg_client.py        # Telethon client 初始化與 session 管理
├── parser.py           # 連結正則解析、轉發偵測
├── Dockerfile
└── __init__.py
```

## handle_task 分派邏輯

```
收到 TaskRequest
  ├─ 偵測到轉發訊息 → parser 取得來源 → transfer_engine 單則搬移
  ├─ 偵測到 TG 連結 → parser 解析 → transfer_engine 單則搬移
  ├─ 偵測到設定指令（如「預設目標改成...」）→ db 更新 config
  ├─ 正在等使用者回應（重試/跳過/一律跳過/確認執行）→ 對應操作
  └─ 其他自然語言 → AI 解析為批量指令 → 回報預估數量 → 等確認
```

## agent.yaml

```yaml
name: tg-transfer-agent
description: "Telegram 群組資源搬移工具"
priority: 3
route_patterns:
  - "搬移|轉存|複製群組|搬到|copy to|transfer|備份群組"
settings:
  default_target_chat: ""
  retry_limit: 3
  progress_interval: 20
  telethon_session: "tg_transfer"
```

## Telethon Session

- Session file 存放在 `/data/tg_transfer/` 掛載目錄
- 首次啟動需登入一次（手機號 + 驗證碼）
- 之後 session 持久化，容器重啟不需重新登入
