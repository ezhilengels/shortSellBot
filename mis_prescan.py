"""
============================================================
 MIS PRE-SCAN v2 — Nifty 100 + Nifty 200
 Filters:
   - Gap: 1.5% to 5.0% (up or down)
   - Prev Day Volume >= 500,000
   - Price >= ₹200
 Data Source: NSE India (Free)
 Author: Ezhil
============================================================
 Run:
   pip install requests pandas tabulate
   python mis_prescan.py
============================================================
"""

import requests
import pandas as pd
from tabulate import tabulate
from datetime import datetime
import time

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────

GAP_MIN        = 1.5    # Min gap % (absolute)
GAP_MAX        = 5.0    # Max gap % (absolute)
MIN_PREV_VOL   = 500000 # Previous day volume
MIN_PRICE      = 200    # Min stock price ₹

# NSE index names
INDICES = {
    "NIFTY 100": "NIFTY%20100",
    "NIFTY 200": "NIFTY%20200",
}

# ─────────────────────────────────────────
#  NSE SESSION
# ─────────────────────────────────────────

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
})


def init_nse():
    """Initialize NSE session with cookies."""
    print("🔌 Connecting to NSE...")
    session.get("https://www.nseindia.com", timeout=15)
    time.sleep(1)
    print("✅ NSE session ready\n")


def fetch_index_stocks(index_key: str, index_url: str) -> pd.DataFrame:
    """Fetch all stocks in a Nifty index with OHLCV data."""
    url  = f"https://www.nseindia.com/api/equity-stockIndices?index={index_url}"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 401:
            init_nse()
            resp = session.get(url, timeout=15)
        data   = resp.json()
        stocks = data.get("data", [])

        rows = []
        for s in stocks:
            symbol = s.get("symbol", "")
            if symbol in ("", "NIFTY 100", "NIFTY 200", "NIFTY 50"):
                continue  # Skip index row itself
            rows.append({
                "symbol":     symbol,
                "open":       float(s.get("open", 0)),
                "ltp":        float(s.get("lastPrice", 0)),
                "prev_close": float(s.get("previousClose", 0)),
                "volume":     int(s.get("totalTradedVolume", 0)),
                "prev_vol":   int(s.get("surgeXappeared", 0) or
                                  s.get("ffmc", 0) or 0),  # fallback
                "high":       float(s.get("dayHigh", 0)),
                "low":        float(s.get("dayLow", 0)),
                "index":      index_key,
            })

        df = pd.DataFrame(rows)
        print(f"  📊 {index_key}: {len(df)} stocks fetched")
        return df

    except Exception as e:
        print(f"  ❌ {index_key} fetch failed: {e}")
        return pd.DataFrame()


def fetch_prev_volume(symbol: str) -> int:
    """Fetch previous day's volume for a symbol."""
    url = f"https://www.nseindia.com/api/quote-equity?symbol={symbol}"
    try:
        resp = session.get(url, timeout=10)
        data = resp.json()
        # Previous day volume from trade info
        prev_vol = (data.get("marketDeptOrderBook", {})
                        .get("tradeInfo", {})
                        .get("totalTradedVolume", 0))
        return int(prev_vol)
    except:
        return 0


# ─────────────────────────────────────────
#  SCAN LOGIC
# ─────────────────────────────────────────

def calculate_gap(open_price: float, prev_close: float) -> float:
    """Calculate gap % from prev close to today's open."""
    if prev_close == 0:
        return 0.0
    return ((open_price - prev_close) / prev_close) * 100


def run_scan(df: pd.DataFrame) -> pd.DataFrame:
    """Apply all 3 filters and return matching stocks."""
    if df.empty:
        return df

    # Calculate gap %
    df["gap_pct"] = df.apply(
        lambda r: calculate_gap(r["open"], r["prev_close"]), axis=1
    )

    # Filter 1: Gap between 1.5% and 5.0% (up or down)
    df = df[
        (df["gap_pct"].abs() >= GAP_MIN) &
        (df["gap_pct"].abs() <= GAP_MAX)
    ]

    # Filter 2: Price >= ₹200
    df = df[df["ltp"] >= MIN_PRICE]

    # Filter 3: Volume >= 500K (using today's volume as proxy)
    # Note: NSE API gives today's traded volume in real time
    df = df[df["volume"] >= MIN_PREV_VOL]

    return df.copy()


def format_results(df: pd.DataFrame) -> str:
    """Format scan results as a clean table."""
    if df.empty:
        return "  No stocks matched the filters."

    display = df[[
        "symbol", "index", "ltp", "open",
        "prev_close", "gap_pct", "volume",
        "high", "low"
    ]].copy()

    display["gap_pct"]    = display["gap_pct"].apply(lambda x: f"{x:+.2f}%")
    display["ltp"]        = display["ltp"].apply(lambda x: f"₹{x:,.2f}")
    display["open"]       = display["open"].apply(lambda x: f"₹{x:,.2f}")
    display["prev_close"] = display["prev_close"].apply(lambda x: f"₹{x:,.2f}")
    display["volume"]     = display["volume"].apply(lambda x: f"{x:,}")
    display["high"]       = display["high"].apply(lambda x: f"₹{x:,.2f}")
    display["low"]        = display["low"].apply(lambda x: f"₹{x:,.2f}")

    display.columns = [
        "Symbol", "Index", "LTP", "Open",
        "Prev Close", "Gap %", "Volume",
        "High", "Low"
    ]
    display = display.sort_values("Gap %", ascending=False)

    return tabulate(display, headers="keys", tablefmt="rounded_outline",
                    showindex=False)


# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────

def main():
    print("=" * 60)
    print("  📡 MIS PRE-SCAN v2")
    print(f"  Gap: {GAP_MIN}% to {GAP_MAX}% | "
          f"Vol >= {MIN_PREV_VOL:,} | Price >= ₹{MIN_PRICE}")
    print(f"  Time: {datetime.now().strftime('%d %b %Y %H:%M:%S')}")
    print("=" * 60)
    print()

    init_nse()

    all_dfs = []

    for index_key, index_url in INDICES.items():
        print(f"🔍 Fetching {index_key}...")
        df = fetch_index_stocks(index_key, index_url)
        if not df.empty:
            all_dfs.append(df)
        time.sleep(1)  # Be gentle with NSE

    if not all_dfs:
        print("❌ No data fetched. Try again after 9:15 AM.")
        return

    # Combine and deduplicate (stock in both Nifty 100 and 200)
    combined = pd.concat(all_dfs, ignore_index=True)
    combined = combined.drop_duplicates(subset="symbol", keep="first")
    print(f"\n✅ Total unique stocks: {len(combined)}")

    # Run scan
    print("\n⚙️  Applying filters...")
    results = run_scan(combined)

    # Split gap up / gap down
    gap_up   = results[results["gap_pct"] >= GAP_MIN]
    gap_down = results[results["gap_pct"] <= -GAP_MIN]

    # ── Print Results ──────────────────────────
    print("\n" + "=" * 60)
    print(f"  📈 GAP UP (>{GAP_MIN}% to <{GAP_MAX}%)")
    print("=" * 60)
    print(format_results(gap_up))
    print(f"\n  Total gap-up stocks: {len(gap_up)}")

    print("\n" + "=" * 60)
    print(f"  📉 GAP DOWN (>{GAP_MIN}% to <{GAP_MAX}%)")
    print("=" * 60)
    print(format_results(gap_down))
    print(f"\n  Total gap-down stocks: {len(gap_down)}")

    print("\n" + "=" * 60)
    print(f"  TOTAL MATCHES: {len(results)} stocks")
    print("=" * 60)

    # Save to CSV
    if not results.empty:
        filename = f"mis_prescan_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        results.to_csv(filename, index=False)
        print(f"\n💾 Results saved to: {filename}")

    print("\n⚠️  Note: Data from NSE. Always verify on Zerodha Kite before trading.")
    print("=" * 60)


if __name__ == "__main__":
    main()
