#!/usr/bin/env bash
set -euo pipefail

echo "Installing dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install kiteconnect requests pandas numpy python-dotenv python-telegram-bot

echo "Done."

cat <<EOF
Next:
1. Create .env with required keys.
2. Run: ./scripts/run_paper.sh
EOF
