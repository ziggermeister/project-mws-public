#!/usr/bin/env python3
"""
mws_fetch_history.py
────────────────────
Fetches/extends mws_ticker_history.csv using Stooq (free, no auth).

Ticker universe is built dynamically from mws_policy.json + mws_tracker.json,
replicating the GAS script's behaviour:
  - All tickers in policy.ticker_constraints (excluding synthetic fixed-price assets)
  - All reporting baselines (active_benchmarks, corr_anchor_ticker)
  - All tickers in mws_tracker.json positions[] / tickers[]
  - Purges history rows for tickers no longer in the required set

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
import json
import sys
import time
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set

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

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
POLICY_FILE  = BASE_DIR / "mws_policy.json"
TRACKER_FILE = BASE_DIR / "mws_tracker.json"
OUT_FILE     = BASE_DIR / "mws_ticker_history.csv"

START_DATE = "2019-01-01"

# ── Fallback hardcoded lists (used only if policy/tracker cannot be loaded) ───
_FALLBACK_PORTFOLIO = [
    "VTI", "VXUS",
    "SOXQ", "CHAT", "BOTZ", "DTCR", "GRID",
    "IAUM", "SIVR",
    "URNM", "REMX", "COPX",
    "ITA", "XLE",
    "XBI",
    "IBIT",
    "DBMF", "KMLM",
]
_FALLBACK_REFERENCE = [
    "SPY", "QQQ", "GLD", "SLV", "AGG",
    "IBB", "SOXX", "PICK", "SRVR",
    "^VIX",
]

# ── Stooq ─────────────────────────────────────────────────────────────────────
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

# Tickers whose short history is expected (suppress the <1000-row warning)
SHORT_HISTORY_EXPECTED = {
    "CHAT", "DTCR", "GRID", "SOXQ", "DBMF", "KMLM", "IBIT", "IAUM"
}


# ── Dynamic ticker universe ───────────────────────────────────────────────────

def build_ticker_universe(policy: dict, tracker: dict) -> List[str]:
    """
    Build the required ticker universe from mws_policy.json + mws_tracker.json,
    replicating the GAS script's getPolicyRequiredTickers_ + normalizeInventory_.

    Sources (union of all):
      1. policy.ticker_constraints keys — every ticker the policy mentions
      2. policy.governance.reporting_baselines (active_benchmarks + corr_anchor_ticker)
      3. tracker.positions[].ticker  (or tracker.tickers[] for older format)

    Exclusions:
      - policy.governance.fixed_asset_prices keys (CASH, TREASURY_NOTE, etc.) —
        these are synthetic assets with no Stooq market data.
      - Anything that fails basic ticker validation (spaces, empty strings, etc.)

    The returned list preserves a stable order: policy tickers first (sorted),
    then any tracker-only tickers appended (sorted), for reproducible output.
    """
    # Synthetic assets that have no market price feed
    fixed_prices = (
        policy.get("governance", {})
              .get("fixed_asset_prices", {}) or {}
    )
    synthetic: Set[str] = {str(t).strip().upper() for t in fixed_prices}

    required: Set[str] = set()

    # 1. All tickers in ticker_constraints (any lifecycle stage)
    for t in (policy.get("ticker_constraints", {}) or {}):
        T = str(t).strip().upper()
        if T and T not in synthetic:
            required.add(T)

    # 2. Reporting baselines
    bl = (
        policy.get("governance", {})
              .get("reporting_baselines", {}) or {}
    )
    for t in (bl.get("active_benchmarks") or []):
        T = str(t).strip().upper()
        if T and T not in synthetic:
            required.add(T)
    if bl.get("corr_anchor_ticker"):
        T = str(bl["corr_anchor_ticker"]).strip().upper()
        if T and T not in synthetic:
            required.add(T)

    # 3. Tracker positions (supports both list-of-objects and list-of-strings)
    raw_positions = tracker.get("positions") or tracker.get("tickers") or []
    for item in raw_positions:
        if isinstance(item, dict):
            T = str(item.get("ticker", "")).strip().upper()
            cls = str(item.get("classification", "")).strip().lower()
            # Skip reference-only tickers that are already captured via baselines
            # (keep them if they appear in policy ticker_constraints)
            if not T or T in synthetic:
                continue
            # Always include; reference tickers are still needed for analytics
            required.add(T)
        elif isinstance(item, str):
            T = item.strip().upper()
            if T and T not in synthetic:
                required.add(T)

    # Basic validation: reject anything that can't be a real ticker
    valid = {t for t in required if t and len(t) <= 30 and " " not in t}

    # Normalize display names back to fetch names
    # e.g. tracker stores "INDEXCBOE:VIX" (display) → we need "^VIX" (fetch name)
    inv_display = {v.upper(): k for k, v in DISPLAY_OVERRIDE.items()}
    valid = {inv_display.get(t, t) for t in valid}

    # Stable ordering: sort the full set
    return sorted(valid)


def load_universe() -> List[str]:
    """
    Load policy + tracker and return the dynamic ticker universe.
    Falls back to the hardcoded lists with a warning if files are missing/corrupt.
    """
    errors = []

    policy = None
    if POLICY_FILE.exists():
        try:
            policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"mws_policy.json: {e}")
    else:
        errors.append("mws_policy.json not found")

    tracker = None
    if TRACKER_FILE.exists():
        try:
            tracker = json.loads(TRACKER_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"mws_tracker.json: {e}")
    else:
        errors.append("mws_tracker.json not found")

    if errors:
        print(f"⚠️  Falling back to hardcoded ticker lists — could not load: {'; '.join(errors)}")
        return sorted(set(_FALLBACK_PORTFOLIO + _FALLBACK_REFERENCE))

    universe = build_ticker_universe(policy, tracker)
    if not universe:
        print("⚠️  Dynamic universe is empty — falling back to hardcoded lists")
        return sorted(set(_FALLBACK_PORTFOLIO + _FALLBACK_REFERENCE))

    return universe


# ── Stooq fetch ───────────────────────────────────────────────────────────────

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

# Build ticker universe from policy + tracker
ALL_TICKERS = load_universe()
# Map display names back to fetch names for the required set
# (DISPLAY_OVERRIDE maps fetch name → display name; invert for the purge check)
DISPLAY_TO_FETCH = {v: k for k, v in DISPLAY_OVERRIDE.items()}
# The set of display names that are required (used for purge)
REQUIRED_DISPLAY: Set[str] = {
    DISPLAY_OVERRIDE.get(t, t) for t in ALL_TICKERS
}

# Determine fetch start date and load existing history
if INCREMENTAL_DAYS is not None:
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

# ── Purge tickers no longer in the required set ───────────────────────────────
if not existing.empty:
    existing_tickers = set(existing["Ticker"].str.strip().str.upper().unique())
    purge = existing_tickers - {t.upper() for t in REQUIRED_DISPLAY}
    if purge:
        print(f"\n🗑️  Purging {len(purge)} retired ticker(s) from history: {sorted(purge)}")
        existing = existing[~existing["Ticker"].str.strip().str.upper().isin(purge)]
    # Report any tickers newly added to the universe (not yet in history)
    new_tickers = {t.upper() for t in REQUIRED_DISPLAY} - existing_tickers
    if new_tickers:
        print(f"✨  New ticker(s) detected (will be backfilled): {sorted(new_tickers)}")

print(f"\nFetching {len(ALL_TICKERS)} tickers from {fetch_start} to today ...")
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

# ── Merge incremental data with existing history ──────────────────────────────
fresh = pd.concat(frames, ignore_index=True)
fresh["Date"] = pd.to_datetime(fresh["Date"])

if not existing.empty and INCREMENTAL_DAYS is not None:
    # Drop overlap window from existing (already purged above), then concat fresh
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
