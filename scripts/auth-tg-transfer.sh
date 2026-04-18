#!/bin/bash
# 認證 tg-transfer 的獨立 Telethon session
set -euo pipefail

cd "$(dirname "$0")/.."
source .venv/bin/activate

# 統一從 tg-transfer-agent.env 載入所有設定
export $(grep -v '^#' .env/tg-transfer-agent.env | grep -v '^$' | xargs)

# 本地執行時覆蓋 SESSION_DIR（env 中是 Docker 路徑）
export SESSION_DIR="data/session/telegram_user_908/tg_transfer"

echo "=== TG Transfer 認證 ==="
echo "Phone: $TELEGRAM_PHONE"
echo "Session dir: $SESSION_DIR"
echo ""

python scripts/python/auth-tg-transfer.py "$@"
