#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo ".env file not found. Create .env with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, KITE_API_KEY, KITE_REQUEST_TOKEN, TRADING_MODE=paper"
  exit 1
fi

export TRADING_MODE=paper

echo "Starting short trader in PAPER mode..."
python3 short_trader.py
