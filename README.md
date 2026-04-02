# shortSellBot

VWAP rejection short-sell intraday bot based on a gap-down strategy.

## Setup

- Copy `.env.example` (create manually) with keys:
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`
  - `KITE_API_KEY`
  - `KITE_REQUEST_TOKEN`
  - `TRADING_MODE=paper` or `live`

- Install dependencies:
  - `pip3 install kiteconnect requests pandas numpy python-dotenv python-telegram-bot`

## Run

- `caffeinate -i python3 short_trader.py`

## Notes

- Default `TRADING_MODE` is `paper`.
- Strategy scans a watchlist, detects VWAP rejection short setups, sends Telegram alerts.
- Manually confirm by inline button before order placement (or skip).
