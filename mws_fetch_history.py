#!/usr/bin/env python3
"""
mws_fetch_history.py
────────────────────
Fetches/extends mws_ticker_history.csv using Stooq (free, no auth).

Modes:
    Full (default):
        python3 mws_fetch_history.py
        Fetches from 2019-01-01 to today. Use once to bootstrap history.

    Incremental:
        python3 mws_fetch_history.py --days 60
        Fetches the last N days only and merges into the existing CSV.
        Used by the daily GitHub Actions workflow for efficient daily updates.

NOTE: Stooq provides split-adjusted + dividend-adjusted closing prices
for US ETFs and stocks. The "Close" column is the total-return adjusted
price, equivalent to Yahoo Finance's "Adj Close".
"""

import argparse
import sys
import time
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    import requests
except ImportError:
    sys.exit("requests not installed. Run: pip install requests")

# ── CLI args ──────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="Fetch MWS ticker price history from Stooq")
_parser.add_argument(
    "--days", type=int, default=None, metavar="N",
    help="Incremental mode: fetch only the last N days and merge with existing CSV",
)
_args = _parser.parse_args()
INCREMENTAL_DAYS: Optional[int] = _args.days

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

# Determine fetch start date
if INCREMENTAL_DAYS is not None:
    # Incremental: find last date in existing CSV, fetch from (last - 5 days) as overlap buffer
    if OUT_FILE.exists():
        try:
            existing = pd.read_csv(OUT_FILE, parse_dates=["Date"])
            last_date = existing["Date"].max()
            fetch_start = (last_date - timedelta(days=5)).strftime("%Y-%m-%d")
            print(f"Incremental mode: last date in CSV = {last_date.date()}, "
                  f"fetching from {fetch_start} (5-day overlap buffer)")
        except Exception as e:
            print(f"⚠️  Could not read existing CSV ({e}); falling back to full fetch")
            existing = pd.DataFrame()
            fetch_start = START_DATE
    else:
        print("No existing CSV found; falling back to full fetch")
        existing = pd.DataFrame()
        fetch_start = START_DATE
else:
    existing = pd.DataFrame()
    fetch_start = START_DATE

print(f"Fetching {len(ALL_TICKERS)} tickers from {fetch_start} to today ...")
print("(Source: Stooq.com — no auth required)\n")

frames = []
failed = []

for i, ticker in enumerate(ALL_TICKERS, 1):
    df = fetch_ticker(ticker, fetch_start)
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

# ── Merge incremental data with existing history ────────────────────────────
fresh = pd.concat(frames, ignore_index=True)
fresh["Date"] = pd.to_datetime(fresh["Date"])

if not existing.empty and INCREMENTAL_DAYS is not None:
    # Drop overlap window from existing, then concat fresh on top
    cutoff = fresh["Date"].min()
    base = existing[existing["Date"] < cutoff]
    long = pd.concat([base, fresh], ignore_index=True)
    print(f"\nMerged: {len(base):,} existing rows + {len(fresh):,} fresh rows")
else:
    long = fresh

long = long.sort_values(["Ticker", "Date"]).drop_duplicates(
    subset=["Ticker", "Date"], keep="last"
).reset_index(drop=True)

# ── Write ─────────────────────────────────────────────────────────────────────
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

if INCREMENTAL_DAYS is None:
    short = counts[
        (counts < 1000) &
        (~counts.index.isin(SHORT_HISTORY_EXPECTED))
    ]
    if not short.empty:
        print(f"\n⚠️  Unexpectedly short history (<1000 rows): {short.index.tolist()}")
