"""
============================================================
 SHORT SELL INTRADAY TRADER
 Strategy : Gap Down + VWAP Rejection Short
 Data     : NSE URL Scraping (Free)
 Orders   : Kite Connect Personal API (Free)
 Author   : Ezhil
============================================================
 Logic:
   1. Stock gapped DOWN 1.5% to 5%
   2. Price bounced UP near VWAP (within range)
   3. Last candle is RED (rejection confirmed)
   4. RSI between 45–62 (not oversold, bounce zone)
   5. Volume on bounce candle < avg (weak bounce = good short)

 Entry  → Short at VWAP rejection
 Target → 1.5% below entry (1:2 RR)
 SL     → Above VWAP + buffer

 Run:
   pip3 install kiteconnect requests pandas numpy python-dotenv python-telegram-bot
   caffeinate -i python3 short_trader.py
============================================================
"""

import os
import time
import json
import logging
import requests
import pandas as pd
import numpy as np
import threading
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from typing import Optional

# ─────────────────────────────────────────
#  LOAD ENV
# ─────────────────────────────────────────

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
KITE_API_KEY       = os.getenv("KITE_API_KEY")
KITE_API_SECRET    = os.getenv("KITE_API_SECRET")
KITE_REQUEST_TOKEN = os.getenv("KITE_REQUEST_TOKEN")  # Fresh every morning!
KITE_ACCESS_TOKEN  = os.getenv("KITE_ACCESS_TOKEN")   # Optional persistent, if available
TRADING_MODE       = os.getenv("TRADING_MODE", "paper")  # "live" or "paper"

# ─────────────────────────────────────────
#  WATCHLIST — Gap down candidates
#  Best: Banks, Financials, Heavy-weight stocks
# ─────────────────────────────────────────

WATCHLIST = [
    "HDFCBANK",
    "ICICIBANK",
    "AXISBANK",
    "KOTAKBANK",
    "SBIN",
    "BAJFINANCE",
    "BAJAJFINSV",
    "INDUSINDBK",
    "RELIANCE",
    "TATAMOTORS",
    "MARUTI",
    "ONGC",
    "NTPC",
    "POWERGRID",
    "COALINDIA",
]

SECONDARY_WATCHLIST = [
    "HDFCBANK",
    "ETERNAL",
    "SBIN",
    "LT",
    "INDIGO",
    "M&M",
    "AXISBANK",
    "TMCV",
    "SUNPHARMA",
    "MAZDOCK",
    "SHRIRAMFIN",
    "KOTAKBANK",
    "JIOFIN",
    "ADANIPORTS",
    "BPCL",
    "ADANIENT",
    "PFC",
    "ASIANPAINT",
    "DLF",
    "DRREDDY",
    "TMPV",
    "HINDZINC",
    "ADANIENSOL",
    "LODHA",
    "BSE",
    "MCX",
    "COFORGE",
    "BLUESTARCO",
    "COCHINSHIP",
    "ASTRAL",
    "HINDPETRO",
    "GODREJPROP",
    "BDL",
    "RVNL",
    "INDUSINDBK",
    "AUBANK",
    "INDIANB",
    "JSWENERGY",
    "NYKAA",
    "KPITTECH",
    "LTF",
    "JUBLFOOD",
    "M&MFIN",
    "GRSE",
    "HINDCOPPER",
    "PGEL",
    "ACUTAAS",
    "CDSL",
    "HSCL",
    "TEJASNET",
    "CHENNPETRO",
    "ANGELONE",
    "ANANTRAJ",
    "GMDCLTD",
    "JBMA",
    "BLS",
    "TARIL",
    "PCBL",
    "JWL",
    "FIRSTCRY",
    "JSWINFRA",
]

# ─────────────────────────────────────────
#  STRATEGY PARAMETERS
# ─────────────────────────────────────────

GAP_DOWN_MIN       = 1.5    # Min gap down %
GAP_DOWN_MAX       = 5.0    # Max gap down %
VWAP_TOUCH_RANGE   = 0.6    # Price must be within 0.6% BELOW vwap (bounce zone) - RELAXED
VWAP_BUFFER_PCT    = 0.3    # SL = VWAP + 0.3% buffer above VWAP
RSI_MIN            = 40     # RSI lower bound (not oversold) - RELAXED
RSI_MAX            = 65     # RSI upper bound (overbought = rejection likely) - RELAXED
VOLUME_RATIO_MAX   = 0.9    # Bounce candle volume < 90% avg (weak bounce) - RELAXED
STOP_LOSS_PCT      = 1.5    # SL = 1.5% above entry
REWARD_RATIO       = 2.0    # Target = 2x risk (1:3 RR)
MAX_CAPITAL        = 20000  # Max ₹ per trade
MAX_TRADES         = 3      # Max trades per day
SCAN_INTERVAL      = 300    # Every 5 minutes
TRADE_START        = "09:45"
TRADE_END          = "14:00" # Close shorts by 2 PM for safety

# ─────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(f"logs/short_trader_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────

trade_count    = 0
traded_symbols = set()
prev_close_map = {}
pending        = {}         # Telegram callback → signal
kite           = None
on_confirm_cb  = None

# ─────────────────────────────────────────
#  NSE DATA
# ─────────────────────────────────────────

nse = requests.Session()
nse.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
})


def nse_init():
    try:
        nse.get("https://www.nseindia.com", timeout=10)
        log.info("✅ NSE session ready")
    except Exception as e:
        log.error(f"NSE init failed: {e}")


def get_quote(symbol: str) -> Optional[dict]:
    url = f"https://www.nseindia.com/api/quote-equity?symbol={symbol}"
    try:
        r = nse.get(url, timeout=10)
        if r.status_code == 401:
            nse_init()
            r = nse.get(url, timeout=10)
        d  = r.json()
        pi = d.get("priceInfo", {})
        return {
            "symbol":     symbol,
            "ltp":        float(pi.get("lastPrice", 0)),
            "open":       float(pi.get("open", 0)),
            "prev_close": float(pi.get("previousClose", 0)),
            "high":       float(pi.get("intraDayHighLow", {}).get("max", 0)),
            "low":        float(pi.get("intraDayHighLow", {}).get("min", 0)),
        }
    except Exception as e:
        log.error(f"{symbol}: quote failed — {e}")
        return None


def get_candles(symbol: str) -> pd.DataFrame:
    url = (f"https://www.nseindia.com/api/chart-databyindex?"
           f"index={symbol}&indices=false&preopen=false")
    try:
        r     = nse.get(url, timeout=10)
        data  = r.json()
        graph = data.get("graphData", [])
        if not graph:
            return pd.DataFrame()
        df = pd.DataFrame(graph,
                          columns=["ts", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["ts"], unit="ms")
        df = df.drop(columns=["ts"])
        df = df.astype({"open": float, "high": float,
                        "low":  float, "close": float, "volume": float})
        return df.reset_index(drop=True)
    except Exception as e:
        log.error(f"{symbol}: candles failed — {e}")
        return pd.DataFrame()

# ─────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = 14) -> float:
    if len(series) < period + 1:
        return 50.0
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    return float((100 - (100 / (1 + rs))).iloc[-1])


def calc_vwap(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    tp   = (df["high"] + df["low"] + df["close"]) / 3
    return float((tp * df["volume"]).cumsum().iloc[-1] /
                 df["volume"].cumsum().iloc[-1])


def calc_vol_ratio(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 1.0
    avg = df["volume"].iloc[:-1].mean()
    return float(df["volume"].iloc[-1]) / avg if avg > 0 else 1.0

# ─────────────────────────────────────────
#  SHORT SIGNAL DETECTION
# ─────────────────────────────────────────

def detect_short_signal(symbol: str) -> Optional[dict]:
    """
    Detects VWAP rejection short setup on gap-down stocks.

    All 5 conditions must pass:
      1. Gapped DOWN 1.5%–5% at open
      2. Price bounced up near VWAP (within 0.6% below VWAP)
      3. Last candle is RED (rejection = sellers taking over)
      4. RSI in 40–65 (not oversold, bounce zone only)
      5. Bounce on low volume (weak buyers = good short)
    """

    quote = get_quote(symbol)
    if not quote or quote["ltp"] == 0:
        log.info(f"{symbol}: No quote data. Skip.")
        return None

    df = get_candles(symbol)
    if df.empty or len(df) < 5:
        log.info(f"{symbol}: Insufficient candle data ({len(df)} candles). Skip.")
        return None

    ltp        = quote["ltp"]
    open_price = quote["open"]
    prev_c     = prev_close_map.get(symbol, quote["prev_close"])

    # ── Condition 1: Gap DOWN check ──────────────────────────
    gap_pct = ((open_price - prev_c) / prev_c) * 100
    if not (-GAP_DOWN_MAX <= gap_pct <= -GAP_DOWN_MIN):
        log.info(f"{symbol}: Gap {gap_pct:.2f}% not in down range [{ -GAP_DOWN_MAX:.1f}% to { -GAP_DOWN_MIN:.1f}%]. Skip.")
        return None

    # ── Condition 2: Price near VWAP (bounce up to VWAP) ─────
    vwap = calc_vwap(df)
    if vwap == 0:
        log.info(f"{symbol}: VWAP calculation failed. Skip.")
        return None
    # Price should be just below VWAP (within VWAP_TOUCH_RANGE%)
    # i.e. price is touching VWAP from below but hasn't crossed it
    dist_pct = ((vwap - ltp) / vwap) * 100   # positive = below vwap
    if not (0 <= dist_pct <= VWAP_TOUCH_RANGE):
        log.info(f"{symbol}: LTP ₹{ltp} not near VWAP ₹{vwap:.2f} (dist={dist_pct:.2f}%, range=0-{VWAP_TOUCH_RANGE}%). Skip.")
        return None

    # ── Condition 3: Last candle RED (rejection confirmed) ────
    last = df.iloc[-1]
    if last["close"] >= last["open"]:
        log.info(f"{symbol}: Last candle green (close {last['close']:.2f} >= open {last['open']:.2f}). No rejection. Skip.")
        return None

    # ── Condition 4: RSI in bounce zone (not oversold) ────────
    rsi = calc_rsi(df["close"])
    if not (RSI_MIN <= rsi <= RSI_MAX):
        log.info(f"{symbol}: RSI {rsi:.1f} out of [{RSI_MIN}–{RSI_MAX}]. Skip.")
        return None

    # ── Condition 5: Low volume bounce (weak buyers) ──────────
    vol_r = calc_vol_ratio(df)
    if vol_r > VOLUME_RATIO_MAX:
        log.info(f"{symbol}: Bounce vol {vol_r:.2f}x too high (> {VOLUME_RATIO_MAX}). Strong buyers. Skip.")
        return None

    # ── All conditions passed → Build short signal ────────────
    entry  = ltp
    sl     = round(vwap * (1 + VWAP_BUFFER_PCT / 100), 2)  # SL above VWAP
    risk   = sl - entry
    target = round(entry - (risk * REWARD_RATIO), 2)
    qty    = max(1, int(MAX_CAPITAL / entry))

    signal = {
        "type":      "SHORT",
        "symbol":    symbol,
        "entry":     entry,
        "stop_loss": sl,
        "target":    target,
        "quantity":  qty,
        "capital":   round(entry * qty, 2),
        "gap_pct":   round(gap_pct, 2),
        "vwap":      round(vwap, 2),
        "rsi":       round(rsi, 1),
        "vol_ratio": round(vol_r, 2),
        "dist_vwap": round(dist_pct, 2),
    }

    log.info("=" * 55)
    log.info(f"🔴 SHORT SIGNAL: {symbol}")
    log.info(f"   Entry     : ₹{entry}  (Short here)")
    log.info(f"   Stop Loss : ₹{sl}  (Above VWAP ₹{vwap:.2f})")
    log.info(f"   Target    : ₹{target}  ({REWARD_RATIO}x reward)")
    log.info(f"   Qty       : {qty} shares | Capital ₹{signal['capital']}")
    log.info(f"   Gap Down  : {gap_pct:.2f}%")
    log.info(f"   RSI       : {rsi:.1f} | Vol Ratio: {vol_r:.2f}x")
    log.info("=" * 55)

    return signal

# ─────────────────────────────────────────
#  KITE SETUP
# ─────────────────────────────────────────

def load_saved_access_token() -> Optional[str]:
    try:
        with open("kitetoken.json", "r") as f:
            data = json.load(f)
            return data.get("access_token")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_access_token(token: str):
    with open("kitetoken.json", "w") as f:
        json.dump({"access_token": token, "saved_at": time.time()}, f)


def setup_kite() -> Optional[KiteConnect]:
    if TRADING_MODE == "paper":
        log.info("📋 PAPER MODE — No real orders")
        return None

    if not KITE_API_KEY or not KITE_API_SECRET:
        log.error("❌ Missing Kite credentials. Set KITE_API_KEY and KITE_API_SECRET in .env")
        raise SystemExit(1)

    access_token = KITE_ACCESS_TOKEN or load_saved_access_token()

    k = KiteConnect(api_key=KITE_API_KEY)
    if access_token:
        log.info("🔑 Using existing Kite access token")
        k.set_access_token(access_token)
        return k

    if not KITE_REQUEST_TOKEN:
        log.error("❌ Missing KITE_REQUEST_TOKEN in .env. Generate via Kite login flow")
        raise SystemExit(1)

    try:
        data = k.generate_session(KITE_REQUEST_TOKEN, api_secret=KITE_API_SECRET)
        access_token = data.get("access_token")
        if access_token:
            k.set_access_token(access_token)
            save_access_token(access_token)
            log.info("✅ Kite login successful (LIVE mode) and access token saved")
            return k
        else:
            raise ValueError("No access_token returned by Kite")
    except Exception as e:
        log.error(f"❌ Kite login failed: {e}")
        raise

# ─────────────────────────────────────────
#  ORDER PLACEMENT
# ─────────────────────────────────────────

def place_short_order(signal: dict) -> tuple:
    """
    Place SELL (short) + SL-M BUY order.
    MIS product = intraday auto-squareoff by broker at 3:20 PM.
    """
    global trade_count, traded_symbols

    symbol = signal["symbol"]
    qty    = signal["quantity"]
    sl     = signal["stop_loss"]

    if TRADING_MODE == "paper":
        buy_id = f"PAPER-SHORT-{symbol}"
        sl_id  = f"PAPER-SL-{symbol}"
        log.info(f"📋 [PAPER] SHORT {symbol} x{qty} @ ₹{signal['entry']}")
        log.info(f"📋 [PAPER] SL    {symbol} @ ₹{sl}")
        trade_count += 1
        traded_symbols.add(symbol)
        _log_trade(signal, buy_id, sl_id)
        return buy_id, sl_id

    try:
        # 1. SELL order (short entry)
        sell_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_MIS,
        )
        log.info(f"🔴 SHORT placed | {symbol} x{qty} | ID: {sell_id}")

        time.sleep(1)

        # 2. SL-M BUY order (cover short above VWAP)
        sl_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            order_type=kite.ORDER_TYPE_SLM,
            trigger_price=sl,
            product=kite.PRODUCT_MIS,
        )
        log.info(f"🛡️  BUY SL placed | {symbol} @ ₹{sl} | ID: {sl_id}")

        trade_count += 1
        traded_symbols.add(symbol)
        _log_trade(signal, sell_id, sl_id)
        return sell_id, sl_id

    except Exception as e:
        log.error(f"❌ Short order failed: {e}")
        return None, None


def _log_trade(signal, order_id, sl_id):
    os.makedirs("trades", exist_ok=True)
    file   = f"trades/{datetime.now().strftime('%Y-%m-%d')}.csv"
    exists = os.path.exists(file)
    row    = pd.DataFrame([{
        "datetime":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type":      "SHORT",
        "mode":      TRADING_MODE,
        **signal,
        "order_id":  order_id,
        "sl_id":     sl_id,
    }])
    row.to_csv(file, mode="a", header=not exists, index=False)
    log.info(f"📝 Trade logged → {file}")

# ─────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────

def send_alert(signal: dict):
    key       = f"short_{signal['symbol']}_{int(time.time())}"
    pending[key] = signal

    msg = (
        f"🔴 *SHORT SIGNAL — VWAP REJECTION*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷  Stock      : `{signal['symbol']}`\n"
        f"📉  Entry      : ₹{signal['entry']}  *(Short here)*\n"
        f"🛡  Stop Loss  : ₹{signal['stop_loss']}  *(Above VWAP)*\n"
        f"🎯  Target     : ₹{signal['target']}\n"
        f"📦  Qty        : {signal['quantity']} shares\n"
        f"💵  Capital    : ₹{signal['capital']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📉  Gap Down   : {signal['gap_pct']}%\n"
        f"📊  VWAP       : ₹{signal['vwap']}\n"
        f"📉  RSI        : {signal['rsi']}\n"
        f"🔊  Vol Ratio  : {signal['vol_ratio']}x\n"
        f"📏  Dist VWAP  : {signal['dist_vwap']}%\n"
        f"⏰  Time       : {datetime.now().strftime('%H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️  Mode: *{TRADING_MODE.upper()}*"
    )

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       msg,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🔴 Place Short", "callback_data": f"confirm_{key}"},
                {"text": "❌ Skip",        "callback_data": f"skip_{key}"},
            ]]
        }
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            log.info(f"📲 Telegram alert sent: {signal['symbol']}")
    except Exception as e:
        log.error(f"Telegram error: {e}")


def send_msg(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text, "parse_mode": "Markdown"
        }, timeout=10)
    except:
        pass


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith("confirm_"):
        key    = data.replace("confirm_", "")
        signal = pending.pop(key, None)

        if not signal:
            await query.edit_message_text("⚠️ Signal expired.")
            return

        if signal["symbol"] in traded_symbols:
            await query.edit_message_text(f"⚠️ {signal['symbol']} already traded.")
            return

        if trade_count >= MAX_TRADES:
            await query.edit_message_text(f"⚠️ Max {MAX_TRADES} trades done today.")
            return

        await query.edit_message_text(
            f"⏳ Placing SHORT for *{signal['symbol']}*...",
            parse_mode="Markdown"
        )

        sell_id, sl_id = place_short_order(signal)

        if sell_id:
            msg = (
                f"✅ *SHORT PLACED*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏷  Stock    : `{signal['symbol']}`\n"
                f"📉  Entry    : ₹{signal['entry']}\n"
                f"🛡  SL       : ₹{signal['stop_loss']}\n"
                f"🎯  Target   : ₹{signal['target']}\n"
                f"📦  Qty      : {signal['quantity']}\n"
                f"🆔  Sell ID  : `{sell_id}`\n"
                f"🆔  SL ID    : `{sl_id}`\n"
                f"📊  Trades   : {trade_count}/{MAX_TRADES}"
            )
        else:
            msg = f"❌ *SHORT FAILED* for {signal['symbol']}. Check logs!"

        await query.edit_message_text(msg, parse_mode="Markdown")

    elif data.startswith("skip_"):
        key    = data.replace("skip_", "")
        signal = pending.pop(key, None)
        symbol = signal["symbol"] if signal else "?"
        await query.edit_message_text(f"❌ *Skipped* `{symbol}`",
                                      parse_mode="Markdown")
        log.info(f"⏭️  Skipped: {symbol}")


def run_bot():
    # Ensure the thread has its own asyncio event loop for python-telegram-bot
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("🤖 Telegram bot started")
    # run_polling in a thread cannot register signal handlers (only main thread allowed)
    app.run_polling(close_loop=False, stop_signals=None)

# ─────────────────────────────────────────
#  TIME HELPERS
# ─────────────────────────────────────────

def in_trading_window() -> bool:
    now = datetime.now().strftime("%H:%M")
    return TRADE_START <= now <= TRADE_END


def past_end() -> bool:
    return datetime.now().strftime("%H:%M") > TRADE_END

# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────

def main():
    global kite

    log.info("=" * 55)
    log.info("  🔴 SHORT SELL TRADER — VWAP REJECTION")
    log.info(f"  Mode      : {TRADING_MODE.upper()}")
    log.info(f"  Gap Down  : {GAP_DOWN_MIN}% – {GAP_DOWN_MAX}%")
    log.info(f"  VWAP Range: within {VWAP_TOUCH_RANGE}% below VWAP")
    log.info(f"  RSI Range : {RSI_MIN} – {RSI_MAX}")
    log.info(f"  Max Trades: {MAX_TRADES}")
    log.info(f"  Window    : {TRADE_START} – {TRADE_END}")
    log.info("=" * 55)

    # Init NSE
    nse_init()

    # Load prev close
    overall_watchlist = WATCHLIST + SECONDARY_WATCHLIST
    log.info("📥 Loading previous close prices...")
    for symbol in overall_watchlist:
        q = get_quote(symbol)
        if q:
            prev_close_map[symbol] = q["prev_close"]
            log.info(f"  {symbol}: ₹{prev_close_map[symbol]}")
        time.sleep(0.3)

    # Start Telegram bot
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    time.sleep(2)

    # Startup message
    send_msg(
        f"🔴 *Short Trader Started*\n"
        f"Mode: {TRADING_MODE.upper()}\n"
        f"Watching: {len(overall_watchlist)} stocks (primary {len(WATCHLIST)} + secondary {len(SECONDARY_WATCHLIST)})\n"
        f"Window: {TRADE_START} – {TRADE_END}\n"
        f"Gap filter: {GAP_DOWN_MIN}%–{GAP_DOWN_MAX}% down\n"
        f"Max trades: {MAX_TRADES}"
    )

    log.info(f"\n⏳ Waiting for trade window {TRADE_START}...\n")

    # ── Main scan loop ──────────────────────────────────────
    while True:
        if past_end():
            log.info("⏹  Trade window ended.")
            send_msg(
                f"⏹ *Short Trader Done*\n"
                f"Trades: {trade_count}/{MAX_TRADES}\n"
                f"Symbols: {', '.join(traded_symbols) or 'None'}"
            )
            break

        if trade_count >= MAX_TRADES:
            log.info(f"⏹  Max {MAX_TRADES} trades done.")
            send_msg(f"✅ Max {MAX_TRADES} trades done! Stopping.")
            break

        if not in_trading_window():
            time.sleep(30)
            continue

        overall_watchlist = WATCHLIST + SECONDARY_WATCHLIST
        log.info(
            f"🔍 Scanning {len(overall_watchlist)} stocks for short setup "
            f"(primary {len(WATCHLIST)} + secondary {len(SECONDARY_WATCHLIST)})..."
        )

        scan_signals = []
        for symbol in overall_watchlist:
            if symbol in traded_symbols:
                continue
            if symbol not in prev_close_map:
                continue

            signal = detect_short_signal(symbol)

            if signal:
                scan_signals.append(signal)
                send_alert(signal)
                time.sleep(2)  # Gap between alerts

        # send summary after each scan
        if scan_signals:
            syms = ", ".join([s["symbol"] for s in scan_signals])
            summary = (
                f"🔎 Scan report: {len(scan_signals)} signal(s) found.\n"
                f"Symbols: {syms}\n"
                f"Trades executed: {trade_count}/{MAX_TRADES}\n"
                f"Mode: {TRADING_MODE.upper()}"
            )
        else:
            summary = (
                f"🔎 Scan report: no signals found this round.\n"
                f"Trades executed: {trade_count}/{MAX_TRADES}\n"
                f"Mode: {TRADING_MODE.upper()}"
            )
        send_msg(summary)

        log.info(f"💤 Next scan in {SCAN_INTERVAL // 60} mins...\n")
        time.sleep(SCAN_INTERVAL)

    log.info("🏁 Session complete.")


if __name__ == "__main__":
    main()
