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
from pathlib import Path
import pandas as pd
import numpy as np

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance not installed. Run: pip install yfinance")

# ── Tickers ──────────────────────────────────────────────────────────────────
# Portfolio tickers
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

# Reference / benchmark tickers kept in the history file
REFERENCE = [
    "SPY", "QQQ", "GLD", "SLV", "AGG",
    "IBB", "SOXX", "PICK", "SRVR",
    "^VIX",
]

ALL_TICKERS = PORTFOLIO + REFERENCE

START_DATE = "2019-01-01"
OUT_FILE   = Path(__file__).parent / "mws_ticker_history.csv"

# ── Fetch ─────────────────────────────────────────────────────────────────────
print(f"Fetching {len(ALL_TICKERS)} tickers from {START_DATE} to today ...")
raw = yf.download(
    ALL_TICKERS,
    start=START_DATE,
    auto_adjust=True,
    progress=True,
)

if raw.empty:
    sys.exit("ERROR: yfinance returned no data. Check network connection.")

# Flatten: (Date, Ticker, AdjClose)
close = raw["Close"]
long = (
    close
    .stack(level=0, future_stack=True)
    .reset_index()
    .rename(columns={"level_1": "Ticker", 0: "AdjClose", "Price": "AdjClose"})
)

# Normalise ^VIX ticker name to match existing CSV convention
long["Ticker"] = long["Ticker"].str.replace(r"^\^VIX$", "INDEXCBOE:VIX", regex=True)

long = long.dropna(subset=["AdjClose"])
long["AdjClose"] = long["AdjClose"].round(6)
long = long.sort_values(["Ticker", "Date"])

# ── Write ─────────────────────────────────────────────────────────────────────
long.to_csv(OUT_FILE, index=False)

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n✅  Saved {len(long):,} rows → {OUT_FILE}")
print(f"    Date range : {long['Date'].min().date()} → {long['Date'].max().date()}")
print(f"    Tickers    : {long['Ticker'].nunique()}")
print()
print("Rows per ticker:")
counts = long.groupby("Ticker")["Date"].count().sort_values(ascending=False)
print(counts.to_string())

# ── Quick sanity: flag any ticker with < 1000 rows (less than ~4 trading years)
short = counts[counts < 1000]
if not short.empty:
    print(f"\n⚠️  Short history (<1000 rows) — may have limited inception dates:")
    print(short.to_string())
    print("   (CHAT, DTCR, IBIT, DBMF, KMLM, SOXQ, GRID are expected to be short)")

print("\nNext step:")
print("  git add mws_ticker_history.csv")
print("  git commit -m 'data: extend ticker history to 2019-01-01 for F1 vol clamp validation'")
print("  git push origin main")
print("  Then re-open Claude and run the F1 vol clamp stress test.")
