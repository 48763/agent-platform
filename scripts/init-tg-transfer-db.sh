#!/bin/bash
# 重置 tg-transfer 的 DB 與 tmp 目錄到乾淨狀態。
#
# 流程：
#   1. 停 tg-transfer-agent（避免 SQLite lock / 寫入競爭）
#   2. 用 docker compose run 跑 init 邏輯（見 scripts/python/init-tg-transfer-db.py）
#   3. 把 agent 啟回來
#
# 預設保留 config（default_target_chat 之類不用重設），順便清 tmp/ 斷點殘檔。
# 旗標：
#   --full        同時砍 config
#   --keep-tmp    不動 tmp 目錄
#   --yes / -y    跳過確認
#
# TG session（/data/tg_transfer.session）永遠不動。

set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE="tg-transfer-agent"
WIPE_CONFIG=0
WIPE_TMP=1
ASSUME_YES=0

for arg in "$@"; do
    case "$arg" in
        --full) WIPE_CONFIG=1 ;;
        --keep-tmp) WIPE_TMP=0 ;;
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help)
            sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown flag: $arg" >&2
            exit 2
            ;;
    esac
done

echo "=== init tg-transfer db ==="
echo "service:     $SERVICE"
echo "wipe config: $([ "$WIPE_CONFIG" = "1" ] && echo YES || echo no)"
echo "wipe tmp:    $([ "$WIPE_TMP" = "1" ] && echo YES || echo no)"
echo
echo "TG session (/data/tg_transfer.session) will NOT be touched."
echo

if [ "$ASSUME_YES" != "1" ]; then
    read -r -p "Proceed? [y/N] " ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) echo "aborted."; exit 1 ;;
    esac
fi

echo
echo "[1/3] stopping $SERVICE ..."
docker compose stop "$SERVICE"

echo
echo "[2/3] clearing DB + tmp ..."
# --no-deps: 不帶起 hub；--entrypoint=""：繞過 base_agent 啟動命令改執行 python。
# 旗標透過環境變數傳進 python，避免雙重參數解析。
docker compose run --rm --no-deps \
    -e WIPE_CONFIG="$WIPE_CONFIG" \
    -e WIPE_TMP="$WIPE_TMP" \
    -v "$(pwd)/scripts:/scripts:ro" \
    --entrypoint="" \
    "$SERVICE" \
    python3 /scripts/python/init-tg-transfer-db.py

echo
echo "[3/3] starting $SERVICE ..."
docker compose up -d "$SERVICE"

echo
echo "done. recent logs:"
sleep 2
docker compose logs "$SERVICE" --tail=10
