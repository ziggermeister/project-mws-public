#!/usr/bin/env python3
"""
mws_fetch_history.py
────────────────────
Fetches/extends mws_ticker_history.csv using Stooq.

Price source priority (per day):
  1. Stooq historical  — primary; split+dividend-adjusted EOD closes
                         (lags by ~1 trading day while market is open)
  2. Stooq real-time   — same-day fallback; automatically fetched when the
                         historical endpoint hasn't published today's date yet
                         (returns the latest traded price; no auth required)

Ticker universe is built dynamically from mws_policy.json:
  - All tickers in policy.ticker_constraints (excluding synthetic fixed-price assets)
  - All reporting baselines (active_benchmarks, corr_anchor_ticker)
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

NOTE: Stooq RT prices are not split+dividend-adjusted (same as historical
for same-day use — no corporate action occurs mid-day). The next Stooq
historical fetch will overwrite these rows with official adjusted closes.
"""

import argparse
import json
import sys
import time
import io
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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

def _csv_is_post_close(path: Path) -> bool:
    """
    Local equivalent of mws_analytics._file_is_post_close for standalone use.
    Returns True if the CSV was written after the last market close AND the
    market is not currently open (so skipping is safe).
    """
    if not path.exists():
        return False
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        try:
            from backports.zoneinfo import ZoneInfo
        except ImportError:
            return False

    ET     = ZoneInfo("America/New_York")
    now_et = datetime.now(ET)

    # During live market hours: never skip (RT prices are changing)
    if now_et.weekday() < 5:
        today_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        if now_et < today_close:
            return False

    # Find last trading close
    d = now_et.date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    close_et = datetime(d.year, d.month, d.day, 16, 0, 0, tzinfo=ET)
    if d == now_et.date() and now_et < close_et:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        close_et = datetime(d.year, d.month, d.day, 16, 0, 0, tzinfo=ET)

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=ET)
    return mtime >= close_et


# ── Parallel fetch config ─────────────────────────────────────────────────────
MAX_FETCH_WORKERS   = 5     # concurrent Stooq connections
REQUEST_INTERVAL    = 0.10  # minimum seconds between HTTP requests (global, ~10 req/s)

_rate_lock          = threading.Lock()
_last_fetch_time: List[float] = [0.0]  # mutable singleton — shared across threads

def _throttled_sleep() -> None:
    """Global rate limiter: enforces REQUEST_INTERVAL between any two HTTP calls."""
    with _rate_lock:
        now  = time.time()
        wait = REQUEST_INTERVAL - (now - _last_fetch_time[0])
        if wait > 0:
            time.sleep(wait)
        _last_fetch_time[0] = time.time()


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

def build_ticker_universe(policy: dict) -> List[str]:
    """
    Build the required ticker universe from mws_policy.json alone.

    Sources (union of all):
      1. policy.ticker_constraints keys — every ticker the policy mentions
      2. policy.governance.reporting_baselines (active_benchmarks + corr_anchor_ticker)

    Exclusions:
      - policy.governance.fixed_asset_prices keys (CASH, TREASURY_NOTE, etc.) —
        these are synthetic assets with no Stooq market data.
      - Anything that fails basic ticker validation (spaces, empty strings, etc.)
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

    # Basic validation: reject anything that can't be a real ticker
    valid = {t for t in required if t and len(t) <= 30 and " " not in t}

    # Stable ordering: sort the full set
    return sorted(valid)


def load_universe() -> List[str]:
    """
    Load mws_policy.json and return the dynamic ticker universe.
    Falls back to the hardcoded lists with a warning if the file is missing/corrupt.
    """
    if not POLICY_FILE.exists():
        print("⚠️  Falling back to hardcoded ticker lists — mws_policy.json not found")
        return sorted(set(_FALLBACK_PORTFOLIO + _FALLBACK_REFERENCE))

    try:
        policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  Falling back to hardcoded ticker lists — mws_policy.json: {e}")
        return sorted(set(_FALLBACK_PORTFOLIO + _FALLBACK_REFERENCE))

    universe = build_ticker_universe(policy)
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


# ── Stooq same-day fallback ────────────────────────────────────────────────────
# Stooq's daily history endpoint lags by ~1 trading day. When today's date is
# missing, we hit Stooq's real-time quote endpoint:
#   https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&e=csv
# which returns the latest traded price (intraday during hours, official close
# after ~9 PM UTC). Same source as history — no auth, no rate limits.

STOOQ_RT_URL = "https://stooq.com/q/l/"


def todays_trading_date() -> str:
    """
    Return today's date string (YYYY-MM-DD) if today is a weekday (Mon–Fri),
    otherwise the most recent weekday. Not holiday-aware.
    """
    candidate = datetime.now(timezone.utc).date()
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate -= timedelta(days=1)
    return candidate.strftime("%Y-%m-%d")


def _fetch_rt_one(ticker: str, target_date: str) -> dict:
    """
    Fetch a single ticker's RT quote from Stooq. Returns a result dict with:
      display, row (dict or None), msg (status string for printing).
    Called in parallel by fetch_today_stooq_rt.
    """
    _throttled_sleep()
    display = DISPLAY_OVERRIDE.get(ticker, ticker)
    symbol  = stooq_symbol(ticker)
    params  = {"s": symbol, "f": "sd2t2ohlcv", "e": "csv"}
    try:
        resp = requests.get(STOOQ_RT_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        text = resp.text.strip()
        if not text or "No data" in text or len(text) < 10:
            return {"display": display, "row": None, "msg": "no data"}

        line  = text.splitlines()[-1]
        parts = line.split(",")
        if len(parts) < 7:
            return {"display": display, "row": None, "msg": f"unexpected format: {text[:60]}"}

        row_date  = parts[1].strip()
        close_str = parts[6].strip()
        if row_date != target_date:
            return {"display": display, "row": None, "msg": f"returned {row_date}, expected {target_date}"}

        price = round(float(close_str), 6)
        row   = {"Date": pd.Timestamp(target_date), "Ticker": display, "AdjClose": price}
        return {"display": display, "row": row, "msg": f"${price:.4f} @ {parts[2].strip()}"}

    except Exception as e:
        return {"display": display, "row": None, "msg": f"error: {type(e).__name__}: {e}"}


def fetch_today_stooq_rt(tickers: List[str], target_date: str) -> pd.DataFrame:
    """
    Fetch today's latest price for each ticker from Stooq's real-time quote
    endpoint. Returns a DataFrame with [Date, Ticker, AdjClose] for rows where
    the returned date matches target_date.

    CSV format returned by Stooq RT: Symbol,Date,Time,Open,High,Low,Close,Volume
    We use Close as AdjClose (same-day: no dividend adjustment needed; the next
    Stooq historical fetch will overwrite with official adjusted closes).

    Requests are issued in parallel (MAX_FETCH_WORKERS threads) with the shared
    global rate limiter to stay polite to Stooq.
    """
    print(f"\n📡  Stooq daily history lagged — fetching {target_date} quotes from Stooq RT ...")

    # Fetch in parallel; collect results keyed by ticker for stable print order
    _rt_results: dict = {}
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as _pool:
        _futures = {_pool.submit(_fetch_rt_one, t, target_date): t for t in tickers}
        for _fut in as_completed(_futures):
            res = _fut.result()
            _rt_results[res["display"]] = res

    rows = []
    for ticker in tickers:
        display = DISPLAY_OVERRIDE.get(ticker, ticker)
        res     = _rt_results.get(display, {"display": display, "row": None, "msg": "no result"})
        if res["row"] is not None:
            rows.append(res["row"])
            print(f"    {display:<14} — Stooq RT: {res['msg']}")
        else:
            print(f"    {display:<14} — Stooq RT: {res['msg']}")

    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["Date", "Ticker", "AdjClose"])


# ── Main ───────────────────────────────────────────────────────────────────────

# ── Benchmark timing ──────────────────────────────────────────────────────────
_BENCH: dict = {"t0": time.perf_counter()}

# Build ticker universe from policy + tracker
ALL_TICKERS = load_universe()
_BENCH["universe_loaded"] = time.perf_counter()
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
            existing = pd.read_csv(OUT_FILE, index_col="Date", parse_dates=True)
            last_date = existing.index.max()
            fetch_start = (last_date - timedelta(days=5)).strftime("%Y-%m-%d")
            print(f"Incremental mode: last date in CSV = {last_date.date()}, "
                  f"fetching from {fetch_start} (5-day overlap buffer)")
            # Fast exit: skip fetch if CSV content already covers the expected
            # last trading date AND the ticker universe matches the required set.
            # Fix: previously only checked the date. If policy added/removed tickers
            # since the last fetch the script would exit without backfilling or purging,
            # causing missing-history errors and stale ranking inputs.
            _existing_cols = {c.strip().upper() for c in existing.columns}
            _required_cols = {t.upper() for t in REQUIRED_DISPLAY}
            _universe_ok   = (_existing_cols == _required_cols)
            if not _universe_ok:
                print(f"⚠️  Ticker universe changed (CSV has {len(_existing_cols)}, "
                      f"policy requires {len(_required_cols)}) — forcing fetch")
            if last_date.strftime("%Y-%m-%d") >= todays_trading_date() and _universe_ok:
                print(f"✅  {OUT_FILE.name} is already up-to-date through {last_date.date()} — no fetch needed")
                # Write a sentinel timing entry so mws_benchmark.py knows we fast-exited
                _fast_timing = BASE_DIR / "mws_benchmark_timing.json"
                try:
                    import json as _jf
                    _et: dict = {}
                    if _fast_timing.exists():
                        try:
                            _et = _jf.loads(_fast_timing.read_text(encoding="utf-8"))
                        except Exception:
                            pass
                    _et["fetch_history"] = {
                        "total_s": 0.0, "fast_exit": True,
                        "universe_load_s": 0.0, "parallel_fetch_s": 0.0,
                        "rt_fallback_s": 0.0, "merge_write_s": 0.0,
                        "tickers_fetched": 0, "tickers_ok": 0,
                        "rows_written": len(existing) if not existing.empty else 0,
                        "mode": "incremental (fast-exit — content up-to-date)",
                    }
                    _fast_timing.write_text(_jf.dumps(_et, indent=2), encoding="utf-8")
                except Exception:
                    pass
                import sys as _sys; _sys.exit(0)
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
    existing_tickers = {c.strip().upper() for c in existing.columns}
    purge = existing_tickers - {t.upper() for t in REQUIRED_DISPLAY}
    if purge:
        print(f"\n🗑️  Purging {len(purge)} retired ticker(s) from history: {sorted(purge)}")
        existing = existing.drop(columns=[c for c in existing.columns if c.strip().upper() in purge])
    # Report any tickers newly added to the universe (not yet in history)
    new_tickers = {t.upper() for t in REQUIRED_DISPLAY} - existing_tickers
    if new_tickers:
        print(f"✨  New ticker(s) detected (will be backfilled): {sorted(new_tickers)}")

print(f"\nFetching {len(ALL_TICKERS)} tickers from {fetch_start} to today ...")
print("(Source: Stooq.com — no auth required)\n")

frames = []
failed = []

def _fetch_one(ticker: str) -> tuple:
    """Thread worker: rate-limit then fetch historical prices for one ticker."""
    _throttled_sleep()
    return ticker, fetch_ticker(ticker, fetch_start)

# Parallel fetch — results collected into a dict, then printed in stable order
_fetch_results: dict = {}
with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as _pool:
    _futures = {_pool.submit(_fetch_one, t): t for t in ALL_TICKERS}
    for _fut in as_completed(_futures):
        _ticker, _df = _fut.result()
        _fetch_results[_ticker] = _df

for i, ticker in enumerate(ALL_TICKERS, 1):
    df = _fetch_results.get(ticker)
    if df is None or df.empty:
        print(f"  [{i:2d}/{len(ALL_TICKERS)}] {ticker:<12} — NO DATA")
        failed.append(ticker)
    else:
        frames.append(df)
        print(f"  [{i:2d}/{len(ALL_TICKERS)}] {ticker:<12} — "
              f"{len(df):>5} rows  "
              f"({df['Date'].min().date()} → {df['Date'].max().date()})")

_BENCH["fetch_done"] = time.perf_counter()

if not frames:
    sys.exit("\nERROR: No data fetched. Check: curl -s 'https://stooq.com/q/d/l/?s=spy.us&d1=20240101&d2=20240110&i=d'")

# ── Same-day fallback: Yahoo Finance when Stooq is lagged ─────────────────────
# Check if Stooq's latest date is behind the expected last market close.
# If so, fetch today's closes from Yahoo Finance and append them.
_fresh_latest = pd.concat(frames)["Date"].max().date().strftime("%Y-%m-%d")
_expected_date = todays_trading_date()

if _fresh_latest < _expected_date:
    print(f"\n⚠️  Stooq latest: {_fresh_latest}  |  Expected: {_expected_date}")
    rt_rows = fetch_today_stooq_rt(ALL_TICKERS, _expected_date)
    if not rt_rows.empty:
        frames.append(rt_rows)
        print(f"    ✅ Injected {len(rt_rows)} Stooq RT rows for {_expected_date}")
    else:
        print(f"    ⚠️  Stooq RT returned no prices for {_expected_date} "
              f"(market may not have traded today)")
else:
    print(f"\n✅  Stooq up-to-date through {_fresh_latest}")

_BENCH["rt_done"] = time.perf_counter()

# ── Merge incremental data with existing history ──────────────────────────────
fresh = pd.concat(frames, ignore_index=True)
fresh["Date"] = pd.to_datetime(fresh["Date"])

if not existing.empty and INCREMENTAL_DAYS is not None:
    # fresh is still long format; pivot to wide first
    fresh_wide = fresh.pivot_table(index="Date", columns="Ticker", values="AdjClose", aggfunc="last")
    fresh_wide.index = pd.to_datetime(fresh_wide.index)
    cutoff = fresh_wide.index.min()
    base = existing[existing.index < cutoff]
    # Merge on union of columns
    all_cols = sorted(set(base.columns) | set(fresh_wide.columns))
    combined = pd.concat([base.reindex(columns=all_cols), fresh_wide.reindex(columns=all_cols)]).sort_index()
    print(f"\nMerged: {len(base):,} existing rows + {len(fresh_wide):,} fresh rows")
    long = combined  # wide format already
else:
    long = fresh  # still long format — will be pivoted in the write step below

# ── Convert to wide format if still in long format (full fetch path) ──────────
if "Ticker" in long.columns:
    long = long.sort_values(["Ticker", "Date"]).drop_duplicates(subset=["Ticker", "Date"], keep="last")
    long = long.pivot_table(index="Date", columns="Ticker", values="AdjClose", aggfunc="last").sort_index()
else:
    # Already wide (incremental merge path) — just ensure sorted
    long = long.sort_index()

long.index.name = "Date"
long.columns.name = None

# ── Write ─────────────────────────────────────────────────────────────────────
long.to_csv(OUT_FILE)
_BENCH["write_done"] = time.perf_counter()

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"✅  Saved {len(long):,} rows → {OUT_FILE.name}")
print(f"    Date range : {long.index.min().date()} → {long.index.max().date()}")
print(f"    Tickers    : {long.shape[1]}")

if failed:
    print(f"\n⚠️  Failed ({len(failed)}): {failed}")

counts = long.notna().sum().sort_values(ascending=False)
print("\nRows per ticker:")
print(counts.to_string())

if INCREMENTAL_DAYS is None:
    short = counts[
        (counts < 1000) &
        (~counts.index.isin(SHORT_HISTORY_EXPECTED))
    ]
    if not short.empty:
        print(f"\n⚠️  Unexpectedly short history (<1000 rows): {short.index.tolist()}")

# ── Benchmark timing summary ──────────────────────────────────────────────────
_t_end = time.perf_counter()
_BENCH["total"] = _t_end - _BENCH["t0"]
_BENCH.setdefault("rt_done",    _BENCH.get("fetch_done", _t_end))
_BENCH.setdefault("write_done", _t_end)

_fetch_s  = _BENCH.get("fetch_done",  _BENCH["t0"]) - _BENCH["t0"]
_rt_s     = _BENCH.get("rt_done",     _BENCH["fetch_done"]) - _BENCH.get("fetch_done", _BENCH["t0"])
_merge_s  = _BENCH.get("write_done",  _t_end) - _BENCH.get("rt_done", _BENCH.get("fetch_done", _BENCH["t0"]))
_total_s  = _BENCH["total"]

print(f"\n{'─'*52}")
print(f"⏱  mws_fetch_history timing:")
print(f"   universe load   : {_BENCH.get('universe_loaded', _BENCH['t0']) - _BENCH['t0']:5.2f}s")
print(f"   parallel fetch  : {_fetch_s:5.2f}s  ({len(ALL_TICKERS)} tickers, {len(frames)} successful)")
print(f"   RT fallback     : {_rt_s:5.2f}s")
print(f"   merge + write   : {_merge_s:5.2f}s")
print(f"   TOTAL           : {_total_s:5.2f}s")
print(f"{'─'*52}")

# Write to shared benchmark timing JSON (read by mws_benchmark.py)
import json as _json
_timing_path = BASE_DIR / "mws_benchmark_timing.json"
try:
    _existing_bench: dict = {}
    if _timing_path.exists():
        try:
            _existing_bench = _json.loads(_timing_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    _existing_bench["fetch_history"] = {
        "total_s":       round(_total_s, 4),
        "universe_load_s": round(_BENCH.get("universe_loaded", _BENCH["t0"]) - _BENCH["t0"], 4),
        "parallel_fetch_s": round(_fetch_s, 4),
        "rt_fallback_s": round(_rt_s, 4),
        "merge_write_s": round(_merge_s, 4),
        "tickers_fetched": len(ALL_TICKERS),
        "tickers_ok":    len(frames),
        "rows_written":  len(long),
        "mode":          "incremental" if INCREMENTAL_DAYS is not None else "full",
    }
    _timing_path.write_text(_json.dumps(_existing_bench, indent=2), encoding="utf-8")
except Exception as _te:
    pass  # non-fatal
