"""
============================================================
 SHORT SELL INTRADAY TRADER
 Strategy : Gap Down + VWAP Rejection Short
 Data     : yfinance (primary) + jugaad-trader (fallback) — both free
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
   pip3 install kiteconnect requests pandas numpy python-dotenv python-telegram-bot yfinance jugaad-trader
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
from datetime import datetime, timedelta
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from typing import Optional

# ── Candle data libraries (free, no subscription needed) ──
import yfinance as yf

try:
    from jugaad_trader.nse import NSELive
    _jugaad_available = True
except ImportError:
    _jugaad_available = False

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
#  NSE DATA  —  yfinance (primary) + jugaad-trader (fallback)
#
#  WHY the old approach broke:
#    • NSE chart-databyindex silently changed its response keys
#      and now requires a fully-seeded browser cookie chain.
#    • Kite historical_data needs a paid Historical Data add-on
#      (₹2000/month) — free/basic Connect plans get 403.
#
#  New stack (both completely free, no paid subscriptions):
#    1. yfinance  — Yahoo Finance, symbol = "HDFCBANK.NS"
#                   get_quote()   → fast_info  (live price)
#                   get_candles() → download(interval="5m", period="1d")
#    2. jugaad-trader — NSELive fallback for candles when yfinance
#                       returns empty (e.g. pre-market / holiday edge cases)
# ─────────────────────────────────────────

# jugaad NSELive singleton (initialised once)
_jugaad_nse: Optional[object] = None


def _get_jugaad_nse():
    """Return a cached NSELive instance, or None if not available."""
    global _jugaad_nse
    if not _jugaad_available:
        return None
    if _jugaad_nse is None:
        try:
            _jugaad_nse = NSELive()
            log.info("✅ jugaad-trader NSELive ready")
        except Exception as e:
            log.warning(f"jugaad-trader init failed: {e}")
            return None
    return _jugaad_nse


def _yf_symbol(symbol: str) -> str:
    """Convert bare NSE symbol → Yahoo Finance ticker  e.g. SBIN → SBIN.NS"""
    return f"{symbol}.NS"


def get_quote(symbol: str) -> Optional[dict]:
    """
    Fetch live quote via yfinance fast_info.
    Falls back to jugaad-trader NSELive if yfinance returns zeros.
    """
    # ── Primary: yfinance ──────────────────────────────────────
    try:
        tk   = yf.Ticker(_yf_symbol(symbol))
        fi   = tk.fast_info           # lightweight, no heavy download
        ltp  = float(fi.get("last_price")       or fi.get("lastPrice",       0))
        prev = float(fi.get("previous_close")   or fi.get("previousClose",   0))
        open_= float(fi.get("open")             or 0)
        high = float(fi.get("day_high")         or fi.get("dayHigh",         0))
        low  = float(fi.get("day_low")          or fi.get("dayLow",          0))

        if ltp > 0:
            log.debug(f"{symbol}: yfinance quote OK  ltp={ltp}")
            return {
                "symbol":     symbol,
                "ltp":        ltp,
                "open":       open_,
                "prev_close": prev,
                "high":       high,
                "low":        low,
            }
        log.warning(f"{symbol}: yfinance quote returned zero LTP, trying jugaad")
    except Exception as e:
        log.warning(f"{symbol}: yfinance quote failed — {e}. Trying jugaad")

    # ── Fallback: jugaad-trader NSELive ────────────────────────
    jnse = _get_jugaad_nse()
    if jnse is None:
        log.error(f"{symbol}: jugaad not available, no quote data")
        return None
    try:
        data = jnse.stock_quote(symbol)
        pi   = data.get("priceInfo", {})
        ltp  = float(pi.get("lastPrice", 0))
        if ltp == 0:
            log.error(f"{symbol}: jugaad quote also returned zero")
            return None
        log.info(f"{symbol}: jugaad-trader quote OK  ltp={ltp}")
        return {
            "symbol":     symbol,
            "ltp":        ltp,
            "open":       float(pi.get("open", 0)),
            "prev_close": float(pi.get("previousClose", 0)),
            "high":       float(pi.get("intraDayHighLow", {}).get("max", 0)),
            "low":        float(pi.get("intraDayHighLow", {}).get("min", 0)),
        }
    except Exception as e:
        log.error(f"{symbol}: jugaad quote failed — {e}")
        return None


def _normalise_candle_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure the DataFrame has exactly [time, open, high, low, close, volume]
    with correct dtypes, sorted ascending, index reset.
    """
    df = df.copy()
    # flatten MultiIndex columns that yfinance sometimes returns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]

    # rename 'datetime' / index → 'time'
    if "datetime" in df.columns:
        df = df.rename(columns={"datetime": "time"})
    if "time" not in df.columns:
        df = df.reset_index().rename(columns={df.index.name or "index": "time"})

    # keep only what we need
    needed = ["time", "open", "high", "low", "close", "volume"]
    df = df[[c for c in needed if c in df.columns]].copy()

    df["time"] = pd.to_datetime(df["time"])
    # strip timezone so downstream comparisons don't break
    if df["time"].dt.tz is not None:
        df["time"] = df["time"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)

    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["close"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def _get_candles_yfinance(symbol: str) -> pd.DataFrame:
    """
    Download today's 5-minute candles from Yahoo Finance.
    Uses period='1d' so we always get the current session.
    """
    try:
        raw = yf.download(
            _yf_symbol(symbol),
            period="1d",
            interval="5m",
            progress=False,
            auto_adjust=True,
        )
        if raw is None or raw.empty:
            log.warning(f"{symbol}: yfinance download returned empty DataFrame")
            return pd.DataFrame()

        df = _normalise_candle_df(raw)
        # keep only regular session (09:15 – 15:30 IST)
        df = df[df["time"].dt.time >= datetime.strptime("09:15", "%H:%M").time()]
        df = df[df["time"].dt.time <= datetime.strptime("15:30", "%H:%M").time()]
        df = df.reset_index(drop=True)

        if df.empty:
            log.warning(f"{symbol}: yfinance — no candles after session filter")
            return pd.DataFrame()

        log.info(f"{symbol}: yfinance candles OK  ({len(df)} candles)")
        return df
    except Exception as e:
        log.warning(f"{symbol}: yfinance candles failed — {e}")
        return pd.DataFrame()


def _get_candles_jugaad(symbol: str) -> pd.DataFrame:
    """
    Fetch today's 5-minute candles via jugaad-trader NSELive.
    jugaad returns a list of dicts with keys: CH_TIMESTAMP, open, high, low, close, volume
    """
    jnse = _get_jugaad_nse()
    if jnse is None:
        return pd.DataFrame()
    try:
        # chart_data returns 1-min by default; we resample to 5-min
        data = jnse.chart_data(symbol)
        records = data.get("grapthData") or data.get("graphData") or data.get("data") or []
        if not records:
            log.warning(f"{symbol}: jugaad chart_data returned empty")
            return pd.DataFrame()

        # jugaad format: [[epoch_ms, close], ...] or [{"date":..., ...}]
        if isinstance(records[0], (list, tuple)):
            # compact format — only timestamp + close, can't compute OHLCV
            # convert to minimal df and let caller decide
            df = pd.DataFrame(records, columns=["ts", "close"])
            df["time"]   = pd.to_datetime(df["ts"], unit="ms")
            df["open"]   = df["close"]
            df["high"]   = df["close"]
            df["low"]    = df["close"]
            df["volume"] = 0.0
        else:
            df = pd.DataFrame(records)
            df = _normalise_candle_df(df)

        # resample to 5-min if finer
        df = df.set_index("time")
        df = df.resample("5min").agg({
            "open":   "first",
            "high":   "max",
            "low":    "min",
            "close":  "last",
            "volume": "sum",
        }).dropna(subset=["close"]).reset_index()

        df = df[df["time"].dt.time >= datetime.strptime("09:15", "%H:%M").time()]
        df = df.reset_index(drop=True)

        log.info(f"{symbol}: jugaad-trader candles OK  ({len(df)} candles)")
        return df
    except Exception as e:
        log.warning(f"{symbol}: jugaad candles failed — {e}")
        return pd.DataFrame()


def get_candles(symbol: str) -> pd.DataFrame:
    """
    Public candle fetcher.
    1st attempt : yfinance (Yahoo Finance, free, no auth)
    2nd attempt : jugaad-trader NSELive (free, no auth)
    Both return a normalised DataFrame: [time, open, high, low, close, volume]
    """
    # ── Primary: yfinance ──────────────────────────────────────
    df = _get_candles_yfinance(symbol)
    if not df.empty and len(df) >= 5:
        return df

    log.warning(f"{symbol}: yfinance insufficient, trying jugaad-trader fallback")

    # ── Fallback: jugaad-trader ────────────────────────────────
    df = _get_candles_jugaad(symbol)
    if not df.empty and len(df) >= 5:
        return df

    log.error(f"{symbol}: both yfinance and jugaad-trader returned no usable candles")
    return pd.DataFrame()


def nse_init():
    """
    Legacy stub — kept so existing call in main() doesn't break.
    yfinance and jugaad-trader need no session warm-up.
    """
    yf_ok = True
    try:
        # quick connectivity smoke-test: fetch info for one liquid stock
        t = yf.Ticker("RELIANCE.NS")
        _ = t.fast_info
    except Exception as e:
        log.warning(f"yfinance connectivity check failed: {e}")
        yf_ok = False

    jnse = _get_jugaad_nse()   # initialises singleton + logs result

    if yf_ok or jnse is not None:
        log.info("✅ Data sources ready  (yfinance=%s  jugaad=%s)",
                 "OK" if yf_ok else "FAIL",
                 "OK" if jnse else "FAIL/unavailable")
    else:
        log.error("❌ Both yfinance and jugaad-trader unavailable — candle data will fail")

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
    entry = ltp

    # FIX 1 — tick-round SL and Target to nearest ₹0.05
    # Kite rejects SLM trigger_price that isn't a valid tick multiple.
    def tick_round(price: float) -> float:
        return round(round(price / 0.05) * 0.05, 2)

    sl     = tick_round(vwap * (1 + VWAP_BUFFER_PCT / 100))  # SL above VWAP
    risk   = sl - entry
    target = tick_round(entry - (risk * REWARD_RATIO))        # target below entry

    # FIX 3 — margin-aware quantity
    # Zerodha MIS short requires ~20-40% margin of trade value.
    # We use 40% (conservative) so we never get a margin rejection.
    # Effective buying power = MAX_CAPITAL / MIS_MARGIN_FACTOR
    MIS_MARGIN_FACTOR = 0.40   # assume 40% margin required (adjust if your account has higher leverage)
    effective_capital = MAX_CAPITAL / MIS_MARGIN_FACTOR
    qty = max(1, int(effective_capital / entry))

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
    log.info(f"   Stop Loss : ₹{sl}  (Above VWAP ₹{vwap:.2f})  [tick-rounded]")
    log.info(f"   Target    : ₹{target}  ({REWARD_RATIO}x reward)  [tick-rounded]")
    log.info(f"   Qty       : {qty} shares | Capital ₹{signal['capital']}  [margin-adjusted]")
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
    if not KITE_API_KEY:
        if TRADING_MODE == "paper":
            log.info("📋 PAPER MODE — No Kite credentials, using public NSE data only")
            return None
        log.error("❌ Missing Kite credentials. Set KITE_API_KEY and KITE_API_SECRET in .env")
        raise SystemExit(1)

    if not KITE_API_SECRET:
        log.error("❌ Missing Kite credentials. Set KITE_API_KEY and KITE_API_SECRET in .env")
        raise SystemExit(1)

    access_token = KITE_ACCESS_TOKEN or load_saved_access_token()

    k = KiteConnect(api_key=KITE_API_KEY)
    if access_token:
        log.info(f"🔑 Using existing Kite access token ({TRADING_MODE.upper()} mode)")
        k.set_access_token(access_token)
        return k

    if not KITE_REQUEST_TOKEN:
        if TRADING_MODE == "paper":
            log.info("📋 PAPER MODE — No Kite access token available for candle fallback")
            return None
        log.error("❌ Missing KITE_REQUEST_TOKEN in .env. Generate via Kite login flow")
        raise SystemExit(1)

    try:
        data = k.generate_session(KITE_REQUEST_TOKEN, api_secret=KITE_API_SECRET)
        access_token = data.get("access_token")
        if access_token:
            k.set_access_token(access_token)
            save_access_token(access_token)
            log.info(f"✅ Kite login successful ({TRADING_MODE.upper()} mode) and access token saved")
            return k
        else:
            raise ValueError("No access_token returned by Kite")
    except Exception as e:
        if TRADING_MODE == "paper":
            log.warning(f"Paper mode continuing without Kite data: {e}")
            return None
        log.error(f"❌ Kite login failed: {e}")
        raise

# ─────────────────────────────────────────
#  ORDER PLACEMENT
# ─────────────────────────────────────────

def _verify_order_complete(order_id: str, symbol: str,
                            retries: int = 6, delay: float = 0.5) -> bool:
    """
    Poll Kite order status until COMPLETE or REJECTED.
    Returns True if order reached COMPLETE, False otherwise.
    Prevents placing SL/target against a position that doesn't exist.
    """
    for attempt in range(retries):
        try:
            orders = kite.orders()
            for o in orders:
                if str(o.get("order_id")) == str(order_id):
                    status = o.get("status", "").upper()
                    if status == "COMPLETE":
                        log.info(f"✅ Order {order_id} COMPLETE")
                        return True
                    if status in ("REJECTED", "CANCELLED"):
                        log.error(f"❌ Order {order_id} {status}: "
                                  f"{o.get('status_message', '')}")
                        return False
                    log.info(f"⏳ Order {order_id} status={status} "
                             f"(attempt {attempt+1}/{retries})")
        except Exception as e:
            log.warning(f"Order status check failed: {e}")
        time.sleep(delay)
    log.warning(f"⚠️ Order {order_id} did not reach COMPLETE after "
                f"{retries} checks — treating as incomplete")
    return False


def place_short_order(signal: dict) -> tuple:
    """
    Place 3 orders for a complete short trade bracket:
      1. SELL  MARKET  MIS  — short entry
      2. BUY   SL-M    MIS  — stop loss above VWAP  (tick-rounded trigger)
      3. BUY   LIMIT   MIS  — target exit below entry (tick-rounded price)

    FIX 2: sell order is verified COMPLETE before SL/target are placed,
            preventing orphan cover orders on a failed entry.
    FIX 4: target limit order is now placed so profits are locked automatically.

    Returns (sell_id, sl_id, target_id) — all three or (None, None, None).
    """
    global trade_count, traded_symbols

    symbol = signal["symbol"]
    qty    = signal["quantity"]
    sl     = signal["stop_loss"]   # already tick-rounded
    target = signal["target"]      # already tick-rounded

    # ── PAPER MODE ───────────────────────────────────────────
    if TRADING_MODE == "paper":
        sell_id   = f"PAPER-SHORT-{symbol}"
        sl_id     = f"PAPER-SL-{symbol}"
        target_id = f"PAPER-TGT-{symbol}"
        log.info(f"📋 [PAPER] SHORT  {symbol} x{qty} @ ₹{signal['entry']}")
        log.info(f"📋 [PAPER] SL     {symbol} @ ₹{sl}     (trigger)")
        log.info(f"📋 [PAPER] TARGET {symbol} @ ₹{target} (limit)")
        trade_count += 1
        traded_symbols.add(symbol)
        _log_trade(signal, sell_id, sl_id, target_id)
        return sell_id, sl_id, target_id

    # ── LIVE MODE ────────────────────────────────────────────
    try:
        # ── Step 1: Short entry SELL ──────────────────────────
        sell_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = kite.EXCHANGE_NSE,
            tradingsymbol    = symbol,
            transaction_type = kite.TRANSACTION_TYPE_SELL,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_MARKET,
            product          = kite.PRODUCT_MIS,
        )
        log.info(f"🔴 SELL placed | {symbol} x{qty} | ID: {sell_id}")

        # FIX 2: confirm entry filled before placing cover orders
        if not _verify_order_complete(sell_id, symbol):
            log.error(f"❌ Entry SELL not filled for {symbol} — "
                      f"aborting SL and target to prevent orphan orders")
            return None, None, None

        # ── Step 2: Stop-loss SL-M BUY ───────────────────────
        sl_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = kite.EXCHANGE_NSE,
            tradingsymbol    = symbol,
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_SLM,
            trigger_price    = sl,          # tick-rounded — Kite accepts this
            product          = kite.PRODUCT_MIS,
        )
        log.info(f"🛡️  SL-M BUY placed  | {symbol} @ ₹{sl} (trigger) | ID: {sl_id}")

        time.sleep(0.5)

        # FIX 4: Step 3 — Target LIMIT BUY (locks profit automatically) ──
        target_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = kite.EXCHANGE_NSE,
            tradingsymbol    = symbol,
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_LIMIT,
            price            = target,      # tick-rounded — limit buy at target
            product          = kite.PRODUCT_MIS,
        )
        log.info(f"🎯 TARGET LIMIT BUY | {symbol} @ ₹{target} (limit)  | ID: {target_id}")

        trade_count += 1
        traded_symbols.add(symbol)
        _log_trade(signal, sell_id, sl_id, target_id)
        return sell_id, sl_id, target_id

    except Exception as e:
        log.error(f"❌ Short order failed: {e}")
        return None, None, None


def _log_trade(signal, order_id, sl_id, target_id=None):
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
        "target_id": target_id,
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

        sell_id, sl_id, target_id = place_short_order(signal)

        if sell_id:
            msg = (
                f"✅ *SHORT PLACED*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏷  Stock      : `{signal['symbol']}`\n"
                f"📉  Entry      : ₹{signal['entry']}\n"
                f"🛡  SL         : ₹{signal['stop_loss']}  *(trigger)*\n"
                f"🎯  Target     : ₹{signal['target']}  *(limit)*\n"
                f"📦  Qty        : {signal['quantity']}\n"
                f"🆔  Sell ID    : `{sell_id}`\n"
                f"🆔  SL ID      : `{sl_id}`\n"
                f"🆔  Target ID  : `{target_id}`\n"
                f"📊  Trades     : {trade_count}/{MAX_TRADES}"
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

    kite = setup_kite()

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
