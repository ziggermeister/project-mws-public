#!/usr/bin/env python3
"""
mws_fetch_history.py
────────────────────
Extends mws_ticker_history.csv back to 2019-01-01.
Uses Yahoo Finance chart API directly via requests — does NOT go through
fc.yahoo.com (the yfinance cookie-auth endpoint that is often blocked).

Usage:
    python3 mws_fetch_history.py

After running:
    git add mws_ticker_history.csv
    git commit -m "data: extend ticker history to 2019-01-01 for F1 vol clamp validation"
    git push origin main
"""

import sys
import time
import math
from datetime import datetime, timezone
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

# ── Yahoo Finance v8 chart API (no cookie required) ───────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
FALLBACK_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"


def to_unix(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def fetch_ticker(ticker: str, start: str) -> Optional[pd.DataFrame]:
    display = "INDEXCBOE:VIX" if ticker == "^VIX" else ticker
    params = {
        "period1": to_unix(start),
        "period2": int(datetime.now(timezone.utc).timestamp()),
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }

    for url_template in (BASE_URL, FALLBACK_URL):
        url = url_template.format(ticker=ticker)
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            result = data.get("chart", {}).get("result")
            if not result:
                continue

            r = result[0]
            timestamps = r.get("timestamp", [])
            adjclose = (
                r.get("indicators", {})
                 .get("adjclose", [{}])[0]
                 .get("adjclose", [])
            )

            if not timestamps or not adjclose:
                continue

            dates = pd.to_datetime(timestamps, unit="s", utc=True).tz_localize(None)
            df = pd.DataFrame({
                "Date": dates.normalize(),   # strip time component
                "Ticker": display,
                "AdjClose": [round(v, 6) if v and not math.isnan(v) else None
                             for v in adjclose],
            }).dropna(subset=["AdjClose"])

            return df

        except requests.exceptions.RequestException as e:
            last_err = e
            continue

    return None


# ── Main ───────────────────────────────────────────────────────────────────────
print(f"Fetching {len(ALL_TICKERS)} tickers from {START_DATE} to today ...")
print("(Direct Yahoo Finance chart API — bypasses fc.yahoo.com)\n")

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

    time.sleep(0.25)

if not frames:
    sys.exit("\nERROR: No data fetched. Check network — try: curl https://query1.finance.yahoo.com/")

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
