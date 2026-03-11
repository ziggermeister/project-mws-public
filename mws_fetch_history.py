#!/usr/bin/env python3
"""
mws_fetch_history.py
────────────────────
Extends mws_ticker_history.csv back to 2019-01-01.
Uses Stooq (stooq.com) — free, no auth, returns CSV directly.

NOTE: Stooq provides split-adjusted + dividend-adjusted closing prices
for US ETFs and stocks. The "Close" column in their output is the
total-return adjusted price, equivalent to Yahoo Finance's "Adj Close".

Usage:
    python3 mws_fetch_history.py

After running:
    git add mws_ticker_history.csv
    git commit -m "data: extend ticker history to 2019-01-01 for F1 vol clamp validation"
    git push origin main
"""

import sys
import time
import io
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    import requests
except ImportError:
    sys.exit("requests not installed. Run: pip install requests")

# ── Config ────────────────────────────────────────────────────────────────────
PORTFOLIO = [
    "VTI", "VXUS",
    "SOXQ", "CHAT", "BOTZ", "DTCR", "GRID",
    "IAUM", "SIVR",
    "URNM", "REMX", "COPX",
    "ITA", "XLE",
    "XBI",
    "IBIT",
    "DBMF", "KMLM",
]

REFERENCE = [
    "SPY", "QQQ", "GLD", "SLV", "AGG",
    "IBB", "SOXX", "PICK", "SRVR",
    "^VIX",
]

ALL_TICKERS = PORTFOLIO + REFERENCE

SHORT_HISTORY_EXPECTED = {
    "CHAT", "DTCR", "GRID", "SOXQ", "DBMF", "KMLM", "IBIT", "IAUM"
}

START_DATE = "2019-01-01"
OUT_FILE   = Path(__file__).parent / "mws_ticker_history.csv"

# ── Stooq ─────────────────────────────────────────────────────────────────────
# Stooq ticker format:
#   US ETFs/stocks → lowercase + ".us"  (e.g. vti.us, spy.us)
#   VIX index      → "^vix"
#
# Stooq returns CSV: Date,Open,High,Low,Close,Volume  (newest row first)
# Close = total-return adjusted price

STOOQ_URL = "https://stooq.com/q/d/l/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Display name overrides (what we store in the CSV Ticker column)
DISPLAY_OVERRIDE = {
    "^VIX": "INDEXCBOE:VIX",
}


def stooq_symbol(ticker: str) -> str:
    """Convert standard ticker to Stooq symbol."""
    if ticker == "^VIX":
        return "vix"   # Stooq serves VIX without the caret prefix
    return ticker.lower() + ".us"


def fetch_ticker(ticker: str, start: str) -> Optional[pd.DataFrame]:
    display = DISPLAY_OVERRIDE.get(ticker, ticker)
    symbol  = stooq_symbol(ticker)

    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt   = datetime.now()

    params = {
        "s":  symbol,
        "d1": start_dt.strftime("%Y%m%d"),
        "d2": end_dt.strftime("%Y%m%d"),
        "i":  "d",
    }

    try:
        resp = requests.get(STOOQ_URL, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()

        text = resp.text.strip()
        if not text or "No data" in text or len(text) < 30:
            print(f"    ↳ Stooq: no data returned for {symbol}")
            return None

        df_raw = pd.read_csv(io.StringIO(text))

        if df_raw.empty or "Close" not in df_raw.columns:
            print(f"    ↳ Stooq: unexpected CSV format for {symbol}: {text[:100]}")
            return None

        df = pd.DataFrame({
            "Date":     pd.to_datetime(df_raw["Date"]),
            "Ticker":   display,
            "AdjClose": pd.to_numeric(df_raw["Close"], errors="coerce").round(6),
        }).dropna(subset=["AdjClose"])

        # Stooq returns newest-first; sort ascending
        df = df.sort_values("Date").reset_index(drop=True)

        return df

    except requests.exceptions.RequestException as e:
        print(f"    ↳ Network error for {ticker}: {e}")
        return None
    except Exception as e:
        print(f"    ↳ Unexpected error for {ticker}: {type(e).__name__}: {e}")
        return None


# ── Main ───────────────────────────────────────────────────────────────────────
print(f"Fetching {len(ALL_TICKERS)} tickers from {START_DATE} to today ...")
print("(Source: Stooq.com — no auth required)\n")

frames = []
failed = []

for i, ticker in enumerate(ALL_TICKERS, 1):
    df = fetch_ticker(ticker, START_DATE)
    if df is None or df.empty:
        print(f"  [{i:2d}/{len(ALL_TICKERS)}] {ticker:<12} — NO DATA")
        failed.append(ticker)
    else:
        frames.append(df)
        print(f"  [{i:2d}/{len(ALL_TICKERS)}] {ticker:<12} — "
              f"{len(df):>5} rows  "
              f"({df['Date'].min().date()} → {df['Date'].max().date()})")

    time.sleep(0.4)   # be polite to Stooq

if not frames:
    sys.exit("\nERROR: No data fetched. Check: curl -s 'https://stooq.com/q/d/l/?s=spy.us&d1=20240101&d2=20240110&i=d'")

# ── Write ─────────────────────────────────────────────────────────────────────
long = pd.concat(frames, ignore_index=True)
long = long.sort_values(["Ticker", "Date"])
long.to_csv(OUT_FILE, index=False)

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"✅  Saved {len(long):,} rows → {OUT_FILE.name}")
print(f"    Date range : {long['Date'].min().date()} → {long['Date'].max().date()}")
print(f"    Tickers    : {long['Ticker'].nunique()}")

if failed:
    print(f"\n⚠️  Failed ({len(failed)}): {failed}")

counts = long.groupby("Ticker")["Date"].count().sort_values(ascending=False)
print("\nRows per ticker:")
print(counts.to_string())

short = counts[
    (counts < 1000) &
    (~counts.index.isin(SHORT_HISTORY_EXPECTED))
]
if not short.empty:
    print(f"\n⚠️  Unexpectedly short history (<1000 rows): {short.index.tolist()}")

print("\nNext steps:")
print("  git add mws_ticker_history.csv")
print("  git commit -m 'data: extend ticker history to 2019-01-01 for F1 vol clamp validation'")
print("  git push origin main")
print("  Then tell Claude: ready to run F1")
