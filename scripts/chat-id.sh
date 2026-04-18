#!/bin/bash
# 查詢 Telegram chat ID
cd "$(dirname "$0")/.."
source .venv/bin/activate
export $(grep -v '^#' .env/gateway.env | grep -v '^$' | xargs)
python gateway/list_chats.py
