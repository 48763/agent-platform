# TG Transfer Agent

Telegram 群組間資源搬移工具，支援單則/批量搬移、媒體去重、搜尋、標籤管理。

## 功能

### 搬移

- **貼連結** → 自動下載並上傳到預設目標群組
- **轉發訊息** → 自動轉存到目標群組
- **批量搬移** → 自然語言指令（Gemini Flash 解析），支援時間區間/數量/全部篩選
- **非阻塞批量** → 確認後立即回覆「開始搬移」，進度透過 WS 即時推送
- **斷點續傳** → 中斷後自動從上次進度繼續
- **跨 Job 去重** → 同一對群組不會重複搬移
- **失敗處理** → 重試 N 次後暫停，提供重試/跳過/一律跳過選項
- **可取消** → Hub 後台可終止進行中的批量任務

### 媒體管理

- **SHA-256 精確去重** — 相同檔案不重複上傳
- **pHash 感知相似** — 不同壓縮率的同一張圖也能識別，相似時詢問使用者
- **影片 metadata** — 上傳時自動帶 duration/width/height + supports_streaming，Telegram 正確顯示
- **Album 並行下載** — 同一則 album 內多媒體並行下載，加速處理
- **Album 原子性** — album 內任一媒體下載失敗則整則不上傳
- **關鍵字搜尋** — 搜尋 caption + 標籤，每頁 10 筆翻頁，結果附超連結
- **以圖搜圖** — 貼圖片，用 pHash Hamming distance 比對相似媒體
- **標籤系統** — 自動從 caption 提取 `#tag`，搬移時沿用保存
- **統計 Dashboard** — `/dashboard` HTML 頁面：儲存數量、標籤數、各標籤統計
- **存活檢查** — 背景定時檢查目標訊息是否存在，不存在則清除記錄

## 設定

**agent.yaml settings：**

| 設定 | 預設值 | 說明 |
|------|--------|------|
| `default_target_chat` | `""` | 預設目標群組（可透過 bot 動態修改） |
| `retry_limit` | `3` | 失敗重試次數 |
| `progress_interval` | `20` | 每 N 則回報進度 |
| `liveness_check_interval` | `24` | 存活檢查間隔（小時） |
| `search_page_size` | `10` | 搜尋結果每頁筆數 |
| `phash_threshold` | `10` | pHash 相似閾值（Hamming distance） |

**環境變數（`.env/tg-transfer-agent.env`）：**

```env
HUB_URL=http://hub:9000
AGENT_HOST=tg-transfer-agent
AGENT_PORT=8011
TG_API_ID=YOUR_API_ID
TG_API_HASH=YOUR_API_HASH
DATA_DIR=/data/tg_transfer
```

## 首次認證

```bash
docker compose build tg-transfer-agent
docker compose up tg-transfer-agent -d
docker exec -it agent-tg-transfer-agent-1 sh
# 容器啟動後提示輸入手機號碼 → 驗證碼 → session 持久化
```

## 使用方式

在 Telegram 對 bot 說：

```
# 設定預設目標
預設目標改成 @my_backup

# 單則搬移
https://t.me/channel_name/123

# 批量搬移
把 @old_channel 的內容搬到 @new_channel
搬移 @source 最近 100 則

# 搜尋
搜尋 python 教學
查詢 #影片

# 翻頁
下一頁 / 上一頁

# 統計
統計
```

## 架構

```
agents/tg_transfer/
├── agent.yaml           # 路由、優先級、設定
├── __main__.py          # TGTransferAgent 入口 + dispatch
├── parser.py            # TG 連結解析、轉發偵測、意圖分類
├── chat_resolver.py     # 群組名稱/邀請連結 → entity
├── db.py                # 搬移 Job/Message SQLite（斷點續傳）
├── media_db.py          # 媒體資產/標籤/搜尋 SQLite
├── transfer_engine.py   # 下載/上傳/album/去重/批量/cancel
├── media_utils.py       # ffprobe 影片 metadata 提取
├── hasher.py            # SHA-256 + pHash 計算
├── tag_extractor.py     # #tag 提取
├── search.py            # 搜尋結果格式化 + 翻頁
├── liveness_checker.py  # 背景存活檢查
├── dashboard.py         # 統計 Dashboard（使用 core/agent_dashboard.py 框架）
├── tg_client.py         # 獨立 Telethon client
└── Dockerfile
```

**初始化失敗處理：** 如果 TG_API_ID/TG_API_HASH 未設定或 Telethon 連線失敗，agent 不會 crash，會在 Hub Dashboard 顯示錯誤訊息。設定好後重啟即恢復。

## 資料庫

兩個 DB 層共用同一個 SQLite 檔案：

**db.py（搬移管理）：**
- `jobs` — 搬移任務（來源、目標、篩選條件、狀態）
- `job_messages` — 每則訊息搬移狀態（斷點續傳）
- `config` — 使用者設定（如 default_target_chat）

**media_db.py（媒體資產）：**
- `media` — 媒體記錄（sha256、phash、來源/目標、狀態 pending/uploaded/skipped）
- `tags` — 標籤
- `media_tags` — 媒體與標籤多對多關聯

## 觸發方式

| 觸發 | 行為 | 需確認 |
|------|------|--------|
| 貼訊息連結 | 解析連結，下載原檔，去重檢查，上傳 | 否（相似時詢問） |
| 轉發訊息 | 偵測轉發來源，下載原檔，上傳 | 否 |
| 批量指令 | AI 解析條件，回報預估數量 | 是 |
| 關鍵字/圖片 | 搜尋媒體，回傳結果+連結 | 否 |

## 依賴

- `telethon` — Telegram client
- `aiosqlite` — 非同步 SQLite
- `imagehash` + `Pillow` — pHash 計算
- `ffmpeg` + `ffprobe`（apk）— 影片抽幀 + metadata 提取
