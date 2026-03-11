#!/usr/bin/env python3
"""
mws_fetch_history.py
────────────────────
One-shot script to extend mws_ticker_history.csv back to 2019-01-01.
Run once from the terminal — requires network access.

Usage:
    python3 mws_fetch_history.py

Output:
    mws_ticker_history.csv  (overwritten in-place, full history 2019→today)

After running, commit the updated CSV:
    git add mws_ticker_history.csv
    git commit -m "data: extend ticker history to 2019-01-01 for F1 vol clamp validation"
    git push origin main
"""

import sys
import time
from pathlib import Path
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance not installed. Run: pip install yfinance")

# ── Tickers ──────────────────────────────────────────────────────────────────
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

# Tickers known to have limited history — don't warn on these
SHORT_HISTORY_EXPECTED = {
    "CHAT", "DTCR", "GRID", "SOXQ", "DBMF", "KMLM", "IBIT", "IAUM"
}

START_DATE = "2019-01-01"
OUT_FILE   = Path(__file__).parent / "mws_ticker_history.csv"

# ── Fetch per ticker (avoids batch-download fc.yahoo.com auth issues) ────────
print(f"Fetching {len(ALL_TICKERS)} tickers from {START_DATE} to today ...")
print("(Using per-ticker fetch to avoid batch-download auth issues)\n")

frames = []
failed = []

for i, ticker in enumerate(ALL_TICKERS, 1):
    display = "INDEXCBOE:VIX" if ticker == "^VIX" else ticker
    try:
        hist = yf.Ticker(ticker).history(start=START_DATE, auto_adjust=True)
        if hist.empty:
            print(f"  [{i:2d}/{len(ALL_TICKERS)}] {ticker:<12} — NO DATA")
            failed.append(ticker)
            continue

        # Normalise to flat long-form
        hist = hist[["Close"]].copy()
        hist.index = pd.to_datetime(hist.index).tz_localize(None)  # strip tz
        hist = hist.rename(columns={"Close": "AdjClose"})
        hist["Ticker"] = display
        hist = hist.reset_index().rename(columns={"index": "Date", "Datetime": "Date"})
        hist = hist[["Date", "Ticker", "AdjClose"]].dropna()

        frames.append(hist)
        print(f"  [{i:2d}/{len(ALL_TICKERS)}] {ticker:<12} — {len(hist):>5} rows  "
              f"({hist['Date'].min().date()} → {hist['Date'].max().date()})")

    except Exception as e:
        print(f"  [{i:2d}/{len(ALL_TICKERS)}] {ticker:<12} — ERROR: {e}")
        failed.append(ticker)

    # Small delay to avoid rate-limiting
    time.sleep(0.3)

if not frames:
    sys.exit("\nERROR: No data fetched. Check network connection.")

# ── Combine and write ─────────────────────────────────────────────────────────
long = pd.concat(frames, ignore_index=True)
long["AdjClose"] = long["AdjClose"].round(6)
long = long.sort_values(["Ticker", "Date"])

long.to_csv(OUT_FILE, index=False)

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"✅  Saved {len(long):,} rows → {OUT_FILE}")
print(f"    Date range : {long['Date'].min().date()} → {long['Date'].max().date()}")
print(f"    Tickers    : {long['Ticker'].nunique()}")

if failed:
    print(f"\n⚠️  Failed tickers ({len(failed)}): {failed}")

counts = long.groupby("Ticker")["Date"].count().sort_values(ascending=False)
print("\nRows per ticker:")
print(counts.to_string())

short = counts[(counts < 1000) & (~counts.index.isin(SHORT_HISTORY_EXPECTED))]
if not short.empty:
    print(f"\n⚠️  Unexpectedly short history (<1000 rows):")
    print(short.to_string())

print("\nNext step:")
print("  git add mws_ticker_history.csv")
print("  git commit -m 'data: extend ticker history to 2019-01-01 for F1 vol clamp validation'")
print("  git push origin main")
print("  Then re-open Claude and say: ready to run F1 stress test")
