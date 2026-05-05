#!/bin/bash
# Telegram session 認證（tg-transfer / gateway 通用）
#
# 在 tg-transfer-agent docker image 裡跑 Telethon login，host 不需 python / venv。
# Session 檔直接寫到 host 對應目錄（docker volume 掛載），重啟 service 即生效。
#
# Usage:
#   ./scripts/auth-telegram.sh tg-transfer
#   ./scripts/auth-telegram.sh gateway
#   ./scripts/auth-telegram.sh tg-transfer --code 12345 [--password 2FA]
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  tg-transfer)
    ENV_FILE=".env/tg-transfer-agent.env"
    SESSION_HOST_DIR="data/session/telegram_user_908/tg_transfer"
    SESSION_NAME="tg_transfer"
    ;;
  gateway)
    ENV_FILE=".env/gateway.env"
    SESSION_HOST_DIR="data/session/telegram_user_908/gateway"
    SESSION_NAME="bot_session"
    ;;
  *)
    cat <<EOF
Usage: $0 <tg-transfer|gateway> [--code 12345] [--password 2FA]

Examples:
  $0 tg-transfer
  $0 tg-transfer --code 12345
  $0 gateway --code 12345 --password your_2fa_password
EOF
    exit 1
    ;;
esac
shift

mkdir -p "$SESSION_HOST_DIR"

# 從 env 檔讀取並 export 到當前 shell（給後面 docker run -e 用）
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [ -z "${TELEGRAM_API_ID:-}" ] || [ -z "${TELEGRAM_API_HASH:-}" ] || [ -z "${TELEGRAM_PHONE:-}" ]; then
  echo "錯誤: $ENV_FILE 缺少 TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE"
  exit 1
fi

echo "=== Telegram 認證: $1 ==="
echo "Env file:    $ENV_FILE"
echo "Phone:       $TELEGRAM_PHONE"
echo "Session dir: $SESSION_HOST_DIR"
echo "Session:     $SESSION_NAME"
echo ""

docker compose run --rm --no-deps \
  --entrypoint python \
  -v "$PWD/scripts/python:/auth:ro" \
  -v "$PWD/$SESSION_HOST_DIR:/session" \
  -e TELEGRAM_API_ID -e TELEGRAM_API_HASH -e TELEGRAM_PHONE \
  -e SESSION_DIR=/session \
  -e TELETHON_SESSION="$SESSION_NAME" \
  tg-transfer-agent \
  /auth/auth-telegram.py "$@"
