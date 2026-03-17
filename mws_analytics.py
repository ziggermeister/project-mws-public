import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, List, Set, Any

import numpy as np
import pandas as pd

# Suppress only known-harmless warnings; do not suppress all warnings globally.
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mws")

# Display names for benchmark tickers used in chart labels.
# Add entries here when adding new benchmarks to active_benchmarks in policy.
BENCH_DISPLAY_NAMES: Dict[str, str] = {
    "SPY":  "S&P 500",
    "VTI":  "Total Market",
    "QQQ":  "Nasdaq-100",
    "ONEQ": "Nasdaq Composite",
    "IWM":  "Russell 2000",
    "DIA":  "Dow Jones",
    "EFA":  "Intl Developed",
    "EEM":  "Emerging Markets",
}

def _bench_display(ticker: str) -> str:
    """Human-readable display name for a benchmark ticker."""
    return BENCH_DISPLAY_NAMES.get(ticker.upper(), ticker.upper())

# ── File paths — single source of truth for all scripts ──────────────────────
# mws_runner.py and any other scripts import these constants
# rather than hardcoding filenames independently.
POLICY_FILENAME    = "mws_policy.json"
HOLDINGS_CSV       = "mws_holdings.csv"
HISTORY_CSV        = "mws_ticker_history.csv"
PERF_LOG_CSV       = "mws_recent_performance.csv"
RESULTS_CSV        = "mws_run_results.csv"
MACRO_MD           = "mws_governance.md"
MARKET_CTX_MD      = "mws_market_context.md"
CHART_FILENAME     = "mws_equity_curve.png"
BREADTH_STATE_JSON = "mws_breadth_state.json"


# ==============================================================================
# 0) Price freshness — auto-refresh history before any analysis
# ==============================================================================

def _todays_trading_date() -> str:
    """Most recent weekday (Mon–Fri) in UTC. Not holiday-aware."""
    d = datetime.now(timezone.utc).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _history_is_stale(history_csv: str) -> bool:
    """
    Return True if the latest date in history_csv is older than today's
    expected trading date, meaning prices need to be refreshed.
    """
    try:
        df = pd.read_csv(history_csv, usecols=["Date"], parse_dates=["Date"])
        latest = df["Date"].max().strftime("%Y-%m-%d")
        return latest < _todays_trading_date()
    except Exception:
        return False  # if we can't read the file, let load_system_files handle it


def _refresh_prices() -> None:
    """
    Run mws_fetch_history.py --days 3 to pull the latest prices into
    mws_ticker_history.csv. Called automatically when history is stale.
    """
    fetch_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mws_fetch_history.py")
    if not os.path.exists(fetch_script):
        logger.warning("mws_fetch_history.py not found — skipping price refresh")
        return

    logger.info("📡  History stale — running mws_fetch_history.py --days 3 ...")
    result = subprocess.run(
        [sys.executable, fetch_script, "--days", "3"],
        capture_output=False,   # stream output so user can see progress
        text=True,
    )
    if result.returncode != 0:
        logger.warning("Price refresh exited with non-zero status — proceeding with existing data")


# ==============================================================================
# 1) Robust loading
# ==============================================================================


def sanitize_event_label(label: str) -> str:
    s = str(label or "").strip()

    # Normalize punctuation
    s = s.replace("“", "").replace("”", "")
    s = s.replace("‘", "").replace("’", "")
    s = s.replace("–", "-").replace("—", "-")
    s = s.replace("\u00A0", " ")

    # CSV-safe cleanup
    s = s.replace(",", " - ")
    s = s.replace(";", " - ")
    s = s.replace('"', "")
    s = s.replace("'", "")
    s = re.sub(r"[\r\n]+", " | ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Compact and safe
    if len(s) > 120:
        s = s[:117].rstrip() + "..."

    return s
    
def update_event_labels_by_date(perf_csv_path: str, date_to_label: dict) -> None:
    """
    Update only EventLabel by Date key.
    Leaves all numeric columns untouched.
    """
    df = pd.read_csv(perf_csv_path, dtype=str).fillna("")

    if "Date" not in df.columns:
        raise ValueError("CSV missing Date column")
    if "EventLabel" not in df.columns:
        raise ValueError("CSV missing EventLabel column")

    safe_map = {
        str(k).strip(): sanitize_event_label(v)
        for k, v in date_to_label.items()
        if str(v).strip()
    }

    mask = df["Date"].isin(safe_map.keys())
    df.loc[mask, "EventLabel"] = df.loc[mask, "Date"].map(safe_map)

    # Atomic write: write to temp file then rename to prevent corruption on crash
    dir_ = os.path.dirname(os.path.abspath(perf_csv_path))
    with tempfile.NamedTemporaryFile("w", dir=dir_, suffix=".tmp", delete=False, newline="") as tmp:
        tmp_path = tmp.name
    try:
        df.to_csv(tmp_path, index=False)
        os.replace(tmp_path, perf_csv_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def apply_recent_event_labels(perf_csv_path: str, recent_dates: list[str], label_fn) -> None:
    """
    recent_dates: list of YYYY-MM-DD dates to consider.
    label_fn: function(date_str) -> str | None
    Updates only EventLabel by Date key.
    """
    date_to_label = {}

    for date_str in recent_dates:
        raw = label_fn(date_str)
        if not raw:
            continue
        label = sanitize_event_label(raw)
        if label.upper().startswith("SKIP"):
            continue
        date_to_label[date_str] = label

    if date_to_label:
        update_event_labels_by_date(perf_csv_path, date_to_label)


def recent_calendar_dates(as_of_date: str, days_back: int = 5) -> list[str]:
    d0 = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    return [
        (d0 - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days_back, -1, -1)
    ]
    
def _fatal(msg: str, code: int = 1) -> None:
    logger.critical(msg)
    raise SystemExit(code)

def load_system_files() -> Tuple[dict, pd.DataFrame, pd.DataFrame]:
    required = [POLICY_FILENAME, HOLDINGS_CSV, HISTORY_CSV]
    missing = [f for f in required if not os.path.exists(f)]
    if missing:
        _fatal(f"[FATAL] System halted. Missing files: {missing}")

    logger.info("Phase 0: Loading System Files...")

    # Auto-refresh prices if history is stale before loading anything
    if os.path.exists(HISTORY_CSV) and _history_is_stale(HISTORY_CSV):
        _refresh_prices()

    def _load_json(path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            _fatal(f"[FATAL] {path} is not valid JSON: {e}")
        if not isinstance(obj, dict):
            _fatal(f"[FATAL] {path} must be a JSON object (dict), got {type(obj).__name__}")
        return obj

    policy = _load_json(POLICY_FILENAME)

    try:
        hist = pd.read_csv(HISTORY_CSV)
        hist.columns = [c.strip() for c in hist.columns]
        hold = pd.read_csv(HOLDINGS_CSV)
        hold.columns = [c.strip() for c in hold.columns]
    except Exception as e:
        _fatal(f"[FATAL] CSV Corruption: {e}")

    # Standardize history column names: Date, Ticker, AdjClose
    for col, lower in [("Date", "date"), ("Ticker", "ticker")]:
        if col not in hist.columns:
            for c in hist.columns:
                if c.lower() == lower:
                    hist.rename(columns={c: col}, inplace=True)
                    break

    price_col = next(
        (c for c in ["AdjClose", "adjclose", "Close", "close", "Price", "price"] if c in hist.columns),
        None
    )
    if price_col is None:
        _fatal("[FATAL] HISTORY_CSV missing a price column (AdjClose/Close/Price).")
    if price_col != "AdjClose":
        hist.rename(columns={price_col: "AdjClose"}, inplace=True)

    hist["Date"]     = pd.to_datetime(hist["Date"], errors="coerce")
    hist["Ticker"]   = hist["Ticker"].astype(str).str.strip().str.upper()
    hist["AdjClose"] = pd.to_numeric(hist["AdjClose"], errors="coerce")
    hist = hist.dropna(subset=["Date", "Ticker", "AdjClose"])

    if hist.empty:
        _fatal("[FATAL] HISTORY_CSV has no valid rows after parsing.")

    return policy, hist, hold


# ==============================================================================
# 2) Policy helpers
# ==============================================================================
def get_held_tickers(hold: pd.DataFrame) -> Set[str]:
    if hold is None or hold.empty:
        return set()
    cols = {c.strip().lower(): c for c in hold.columns}
    ticker_col = cols.get("ticker")
    shares_col = cols.get("shares")
    if not ticker_col or not shares_col:
        logger.warning("Holdings CSV missing Ticker/Shares columns. Got: %s", list(hold.columns))
        return set()
    # Vectorized: no iterrows()
    shares_numeric = pd.to_numeric(hold[shares_col], errors="coerce")
    positive_mask  = shares_numeric.fillna(0) > 0
    return set(hold.loc[positive_mask, ticker_col].astype(str).str.strip().str.upper())

def get_policy_required_tickers(policy: dict) -> Set[str]:
    """Benchmarks + all tickers in ticker_constraints. Used for audit/warn.
    Excludes fixed-price synthetic assets (CASH, TREASURY_NOTE, etc.) — they
    are not real market tickers and should never be fetched from price feeds."""
    req: Set[str] = set()
    bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
    for t in (bl.get("active_benchmarks") or []):
        req.add(str(t).strip().upper())
    if bl.get("corr_anchor_ticker"):
        req.add(str(bl["corr_anchor_ticker"]).strip().upper())
    # Fixed-price synthetic assets — authoritative exclusion list
    fixed_prices = (policy.get("governance", {}).get("fixed_asset_prices", {}) or {})
    synthetic = {str(t).strip().upper() for t in fixed_prices}
    for t in (policy.get("ticker_constraints", {}) or {}):
        T = str(t).strip().upper()
        if T not in synthetic:
            req.add(T)
    return {x for x in req if x}

def get_ticker_proxy(policy: dict, ticker: str, default: Optional[str] = None) -> str:
    """
    Returns the benchmark_proxy for a ticker from ticker_constraints.lifecycle.
    Falls back to policy.governance.reporting_baselines.corr_anchor_ticker,
    then to the first active_benchmark, then to "VTI" as last resort.
    Never silently hardcodes a ticker that isn't in the policy.
    """
    tc = policy.get("ticker_constraints", {}) or {}
    proxy = ((tc.get(ticker, {}) or {}).get("lifecycle", {}) or {}).get("benchmark_proxy")
    if proxy:
        return str(proxy).strip().upper()
    if default is not None:
        return str(default).strip().upper()
    # Derive from policy rather than hardcoding
    bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
    anchor = bl.get("corr_anchor_ticker") or (bl.get("active_benchmarks") or [None])[0]
    if anchor:
        return str(anchor).strip().upper()
    return "VTI"  # true last resort; will warn in caller if unexpected

def get_ticker_stage(policy: dict, ticker: str) -> str:
    """Tickers absent from ticker_constraints default to REFERENCE."""
    T = str(ticker).strip().upper()
    tc = policy.get("ticker_constraints", {}) or {}
    if T not in tc:
        return "REFERENCE"
    stage = ((tc[T] or {}).get("lifecycle", {}) or {}).get("stage", "inducted")
    return str(stage).strip().upper()

def get_ticker_sleeve(policy: dict, ticker: str) -> str:
    """Returns primary sleeve name from ticker_to_sleeves, or UNMAPPED."""
    T = str(ticker).strip().upper()
    mapping = (policy.get("ticker_to_sleeves", {}) or {}).get(T)
    if not isinstance(mapping, dict) or not mapping:
        return "UNMAPPED"
    return max(mapping, key=lambda k: mapping[k])


# ==============================================================================
# 3) Audit + valuation
# ==============================================================================
def run_mws_audit(policy: dict, hist: pd.DataFrame, hold: pd.DataFrame):
    logger.info("Phase 1: Starting Titanium Hard-Stop Audit...")

    policy_required = get_policy_required_tickers(policy)
    tc = policy.get("ticker_constraints", {}) or {}
    have_hist = set(hist["Ticker"].unique())

    max_date = hist["Date"].max()
    logger.info("AUDIT: UniverseMax=%s", max_date.date())

    held_set = get_held_tickers(hold)

    # Ranking candidates: inducted/activated tickers with price history
    candidates: Set[str] = set()
    for t in tc:
        stage = get_ticker_stage(policy, t)
        if stage in ["INDUCTED", "ACTIVATED"] and t in have_hist:
            candidates.add(t)

    candidates |= held_set  # always include held for reporting continuity
    final_candidates = sorted(t for t in candidates if t in have_hist)

    missing_from_hist = sorted(policy_required - have_hist)
    if missing_from_hist:
        logger.warning("AUDIT: Policy tickers missing from history: %s", ", ".join(missing_from_hist))

    logger.info("AUDIT: Ranking Universe: %d tickers (Active + Held)", len(final_candidates))
    logger.info("Audit Passed.")
    return final_candidates, [], missing_from_hist


def calculate_portfolio_value(policy: dict, hold: pd.DataFrame, hist: pd.DataFrame) -> Tuple[float, str]:
    """
    Includes policy.governance.fixed_asset_prices for CASH / TREASURY_NOTE / etc.

    Handles both legacy scalar format and v2.7.1+ structured object format:
      Legacy:     "CASH": 1.0
      Structured: "TREASURY_NOTE": { "price_type": "market", "fallback_price": 45000, ... }
    """
    print("[LOG] Phase 2: Calculating Portfolio Value...")

    latest_prices = hist.sort_values("Date").groupby("Ticker").last()["AdjClose"]
    asof = str(hist["Date"].max().date())
    policy_version = policy.get("meta", {}).get("policy_version", "unknown")

    fixed_raw = (policy.get("governance", {}) or {}).get("fixed_asset_prices", {}) or {}

    def _resolve_fixed_price(ticker: str) -> Optional[float]:
        entry = fixed_raw.get(ticker)
        if entry is None:
            return None
        if isinstance(entry, dict):
            price_type = str(entry.get("price_type", "fixed")).strip().lower()
            if price_type == "market":
                live = latest_prices.get(ticker)
                if live is not None and np.isfinite(float(live)) and float(live) > 0:
                    return float(live)
                fallback = entry.get("fallback_price")
                return float(fallback) if fallback is not None else None
            fallback = entry.get("fallback_price")
            return float(fallback) if fallback is not None else None
        try:
            v = float(entry)
            return v if np.isfinite(v) else None
        except (TypeError, ValueError):
            return None

    total_val = 0.0
    if hold is not None and not hold.empty:
        h_cols = {c.strip().lower(): c for c in hold.columns}
        h_ticker_col = h_cols.get("ticker")
        h_shares_col = h_cols.get("shares")
        if not h_ticker_col or not h_shares_col:
            logger.warning("Holdings CSV missing Ticker/Shares columns; portfolio value will be 0. Got: %s", list(hold.columns))
        else:
            # Vectorized: no iterrows()
            tickers = hold[h_ticker_col].astype(str).str.strip().str.upper()
            qtys    = pd.to_numeric(hold[h_shares_col], errors="coerce").fillna(0.0)
            def _get_price(t: str) -> float:
                fp = _resolve_fixed_price(t)
                if fp is not None:
                    return fp
                return float(latest_prices.get(t, 0.0) or 0.0)
            prices    = tickers.map(_get_price)
            total_val = float((qtys * prices).sum())

    print(f"\n🚀 TITANIUM COMMAND CENTER | AS-OF: {asof} | policy={policy_version}")
    print(f"🌍 REGIME: 🟢 BULL | PORTFOLIO: ${total_val:,.2f}")
    return total_val, asof


# ==============================================================================
# 4) Momentum signal helpers (policy-compliant 3-signal blend)
# ==============================================================================

def _compute_tr12m(prices: pd.Series) -> Optional[float]:
    """12-month total return from a price Series (policy weight: 45%)."""
    p = pd.to_numeric(prices, errors="coerce").dropna()
    if len(p) < 2:
        return None
    return float(p.iloc[-1] / p.iloc[0] - 1)


def _compute_slope_6m(prices: pd.Series) -> Optional[float]:
    """Annualized OLS slope of log-price over a 6-month window (policy weight: 35%)."""
    p = pd.to_numeric(prices, errors="coerce").dropna()
    if len(p) < 10:
        return None
    log_p = np.log(p.values.astype(float))
    x     = np.arange(len(log_p), dtype=float)
    slope = float(np.polyfit(x, log_p, 1)[0])  # slope per trading day
    return slope * 252  # annualized


def _compute_residual_3m(t_prices: pd.Series, anchor_prices: pd.Series) -> Optional[float]:
    """3-month total-return residual of ticker minus VTI anchor (policy weight: 20%)."""
    t = pd.to_numeric(t_prices,      errors="coerce").dropna()
    v = pd.to_numeric(anchor_prices, errors="coerce").dropna()
    common = t.index.intersection(v.index)
    if len(common) < 5:
        return None
    t_c, v_c = t.reindex(common), v.reindex(common)
    tr_t = float(t_c.iloc[-1] / t_c.iloc[0] - 1)
    tr_v = float(v_c.iloc[-1] / v_c.iloc[0] - 1)
    return tr_t - tr_v


def _blend_score(
    tr12: Optional[float],
    slope6: Optional[float],
    res3: Optional[float],
    weights: dict,
) -> Optional[float]:
    """Weighted blend with graceful partial-component fallback when data is missing."""
    if not weights:
        return None
    components = [
        (tr12,   weights.get("tr_12m",      0.45)),
        (slope6, weights.get("slope_6m",     0.35)),
        (res3,   weights.get("residual_3m",  0.20)),
    ]
    total_w, total_s = 0.0, 0.0
    for val, w in components:
        if val is not None and np.isfinite(val):
            total_s += val * w
            total_w += w
    if total_w <= 0:
        return None
    return total_s / total_w  # renormalized to available components


# ==============================================================================
# 4b) Legacy helpers (used by charting / alpha calculation)
# ==============================================================================
def _aligned_total_return(prices: pd.Series) -> Optional[float]:
    prices = pd.to_numeric(prices, errors="coerce").dropna()
    if len(prices) < 2:
        return None
    return float(prices.iloc[-1] / prices.iloc[0] - 1)

def compute_alpha_vs_proxy(hist: pd.DataFrame, ticker: str, proxy: str, start_date: pd.Timestamp) -> Optional[float]:
    T, P = ticker.upper(), proxy.upper()
    t_df = hist[(hist["Ticker"] == T) & (hist["Date"] >= start_date)].sort_values("Date")
    p_df = hist[(hist["Ticker"] == P) & (hist["Date"] >= start_date)].sort_values("Date")
    if t_df.empty or p_df.empty:
        return None

    common = np.intersect1d(
        t_df["Date"].values.astype("datetime64[ns]"),
        p_df["Date"].values.astype("datetime64[ns]")
    )
    if len(common) < 2:
        return None

    idx = pd.to_datetime(common)
    tr_t = _aligned_total_return(t_df.set_index("Date").loc[idx]["AdjClose"])
    tr_p = _aligned_total_return(p_df.set_index("Date").loc[idx]["AdjClose"])
    if tr_t is None or tr_p is None:
        return None
    return tr_t - tr_p

def generate_rankings(policy: dict, hist: pd.DataFrame, candidates: List[str], hold: pd.DataFrame) -> pd.DataFrame:
    logger.info("Phase 3: Generating Rankings...")
    if not candidates or hist.empty:
        return pd.DataFrame(columns=["Ticker", "Score", "Pct", "RawScore", "Alpha", "AlphaVs", "Sleeve", "Status"])

    held_set = get_held_tickers(hold)

    # ── Momentum blend weights from policy (fall back to spec defaults) ──────
    _mo = (policy.get("momentum_engine", {}) or {})
    _wt = (_mo.get("signal_weights", {}) or {})
    weights = {
        "tr_12m":      float(_wt.get("tr_12m",      0.45)),
        "slope_6m":    float(_wt.get("slope_6m",    0.35)),
        "residual_3m": float(_wt.get("residual_3m", 0.20)),
    }

    # ── Alpha start date from policy; warn rather than silently hardcode ─────
    _bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
    _alpha_start_str = str(_bl.get("alpha_start_date") or _bl.get("chart_start_date") or "").strip()
    if _alpha_start_str:
        alpha_start = pd.to_datetime(_alpha_start_str, errors="coerce")
        if pd.isna(alpha_start):
            logger.warning(
                "alpha_start_date '%s' in policy is not a valid date; falling back to 2024-01-01",
                _alpha_start_str,
            )
            alpha_start = pd.Timestamp("2024-01-01")
    else:
        logger.warning(
            "No alpha_start_date or chart_start_date in policy.governance.reporting_baselines; "
            "defaulting to 2024-01-01"
        )
        alpha_start = pd.Timestamp("2024-01-01")

    # ── Anchor price series for residual_3m component ────────────────────────
    anchor_ticker = str(policy.get("corr_anchor_ticker", "VTI")).upper()
    anchor_data = hist[hist["Ticker"] == anchor_ticker].sort_values("Date")
    anchor_prices: pd.Series = (
        anchor_data.set_index("Date")["AdjClose"] if not anchor_data.empty else pd.Series(dtype=float)
    )

    # ── Compute raw blend score per candidate ────────────────────────────────
    rows: List[dict] = []
    for t in candidates:
        t_data = hist[hist["Ticker"] == t].sort_values("Date")
        if t_data.empty:
            continue
        prices = t_data.set_index("Date")["AdjClose"]

        tr12   = _compute_tr12m(prices)
        slope6 = _compute_slope_6m(prices)
        res3   = _compute_residual_3m(prices, anchor_prices)
        blend  = _blend_score(tr12, slope6, res3, weights)

        proxy = get_ticker_proxy(policy, t)
        alpha = compute_alpha_vs_proxy(hist, t, proxy, alpha_start)

        rows.append({
            "Ticker":   t,
            "RawScore": blend,
            "Alpha":    "N/A" if alpha is None else f"{alpha:+.1%}",
            "AlphaVs":  proxy,
            "Sleeve":   get_ticker_sleeve(policy, t),
            "Status":   f"{get_ticker_stage(policy, t)}/{'HELD' if t in held_set else 'WATCH'}",
        })

    if not rows:
        return pd.DataFrame(columns=["Ticker", "Score", "Pct", "RawScore", "Alpha", "AlphaVs", "Sleeve", "Status"])

    df = pd.DataFrame(rows)

    # ── Percentile-rank within the inducted universe ──────────────────────────
    # Higher raw score → higher percentile rank (better momentum).
    df["Pct"]   = df["RawScore"].rank(pct=True, na_option="keep")
    df["Score"] = df["Pct"]   # exported Score is the 0–1 percentile rank

    df = df.sort_values("Score", ascending=False).reset_index(drop=True)

    # ── Display table ────────────────────────────────────────────────────────
    w_line = f"{weights['tr_12m']:.0%} TR12m · {weights['slope_6m']:.0%} Slope6m · {weights['residual_3m']:.0%} Res3m"
    logger.info("─" * 74)
    logger.info("🏆  MOMENTUM RANKINGS  (blend: %s)", w_line)
    logger.info("─" * 74)
    logger.info("%-6s %6s %8s %8s %5s %-32s %s",
                "Ticker", "Pct", "Raw", "Alpha", "Vs", "Sleeve", "Status")
    for _, r in df.iterrows():
        sleeve_disp = (r["Sleeve"] or "UNMAPPED")
        if len(sleeve_disp) > 32:
            sleeve_disp = sleeve_disp[:29] + "..."
        pct_str = f"{r['Pct']:.1%}" if pd.notna(r["Pct"]) else "  N/A"
        raw_str = f"{r['RawScore']:.4f}" if pd.notna(r["RawScore"]) else "   N/A"
        logger.info("  %-6s %6s %8s %8s %5s %-32s %s",
                    r["Ticker"], pct_str, raw_str, r["Alpha"], r["AlphaVs"],
                    sleeve_disp, r["Status"])

    return df[["Ticker", "Score", "Pct", "RawScore", "Alpha", "AlphaVs", "Sleeve", "Status"]]


# ==============================================================================
# 5) Portfolio alpha + charting (2-panel)
# ==============================================================================
def _find_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    cols = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in cols:
            return cols[cand.lower()]
    return None

def compute_portfolio_alpha_from_log(policy: dict) -> Dict[str, str]:
    if not os.path.exists(PERF_LOG_CSV):
        return {}

    df = pd.read_csv(PERF_LOG_CSV)
    if df.empty:
        return {}
    df.columns = [c.strip() for c in df.columns]

    date_col = _find_col(df, ["Date", "date"])
    if not date_col:
        return {}

    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col]).sort_values(date_col).drop_duplicates(subset=[date_col], keep="last")

    bl       = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
    benches  = [str(x).strip().upper() for x in (bl.get("active_benchmarks") or []) if x]
    port_col = _find_col(df, ["PortfolioPct", "portfoliopct"])
    if not port_col:
        return {}

    chart_start_str = str(bl.get("chart_start_date") or "").strip()
    chart_start = pd.to_datetime(chart_start_str, errors="coerce") if chart_start_str else None

    dfw = df.copy()
    if chart_start is not None and pd.notna(chart_start):
        dfw = dfw[dfw[date_col] >= chart_start]

    dfw[port_col] = pd.to_numeric(dfw[port_col], errors="coerce")
    dfw = dfw.dropna(subset=[port_col])
    if len(dfw) < 2:
        return {}

    p_last = float(dfw[port_col].iloc[-1])
    out: Dict[str, str] = {}
    for b in benches:
        pct_col = _find_col(df, [f"Pct_{b}", f"pct_{b}", f"pct_{b.lower()}"])
        if not pct_col:
            continue
        s = pd.to_numeric(dfw[pct_col], errors="coerce").dropna()
        if not s.empty:
            out[b] = f"{p_last - float(s.iloc[-1]):+.2%}"

    return out

def _compute_max_drawdown(series: pd.Series) -> float:
    """Peak-to-trough max drawdown from a cumulative return series (e.g. 0.06 = 6%)."""
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 2:
        return 0.0
    # Convert cumulative pct return to index levels (1 + r)
    levels = 1 + s
    rolling_peak = levels.cummax()
    drawdowns = (levels - rolling_peak) / rolling_peak
    return float(drawdowns.min())   # negative value; min = worst

# _rolling_alpha, _backfill_event_labels, _read_chart_events, and rotate_and_chart
# have been moved to mws_charts.py. Import them from there when charting is needed.


# ==============================================================================
# 5b) Performance log updater  (replaces GAS upsertAndRecomputePerformanceLog_)
# ==============================================================================

def _get_scheduled_cash_flow(date_str: str, policy: dict) -> float:
    """
    Return the total scheduled cash flow for a given date (YYYY-MM-DD).
    Negative = withdrawal (e.g. SEPP), positive = contribution.
    Supports recurrence "annual" (same month/day every year) and "once" (exact date).
    Replicates GAS getScheduledCashFlow_.
    """
    flows = policy.get("scheduled_cash_flows") or []
    if not flows:
        return 0.0
    try:
        month = int(date_str[5:7])
        day   = int(date_str[8:10])
    except (ValueError, IndexError):
        return 0.0
    total = 0.0
    for flow in flows:
        amount = float(flow.get("amount", 0) or 0)
        if not amount:
            continue
        rec = str(flow.get("recurrence", "once")).strip().lower()
        if rec == "annual":
            if int(flow.get("month", 0)) == month and int(flow.get("day", 0)) == day:
                total += amount
        else:
            if str(flow.get("date", "")).strip() == date_str:
                total += amount
    return total


def update_performance_log(
    policy: dict,
    hist: pd.DataFrame,
    hold: pd.DataFrame,
    today_total_val: Optional[float] = None,
    perf_log: str = PERF_LOG_CSV,
) -> None:
    """
    Update mws_recent_performance.csv with all trading days not yet logged.
    Replicates GAS upsertAndRecomputePerformanceLog_.

    Strategy (mirrors GAS rolling-recompute approach):
      - Rows already in the log outside the 5-day recompute window:
        PortfolioValue and PortfolioPct are preserved as-is.
      - Rows in the last 5 calendar days + today: recomputed (handles price
        revisions and today's fresh portfolio value).
      - New dates not yet in the log: appended and computed fresh.

    Portfolio value for backfill dates is approximated using current holdings
    x hist prices — accurate when no trades occurred between the last log date
    and today. For today, today_total_val (from calculate_portfolio_value) is
    preferred as it correctly handles TREASURY_NOTE via policy fallback price.
    """
    bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
    chart_start = str(bl.get("chart_start_date", "") or "").strip()
    benches = [str(b).strip().upper() for b in (bl.get("active_benchmarks") or [])]

    if not benches:
        logger.warning("update_performance_log: no active_benchmarks in policy — skipping")
        return
    if not chart_start:
        logger.warning("update_performance_log: no chart_start_date in policy — skipping")
        return

    # ── Fast price lookup: (date_str, TICKER) → float ────────────────────────
    price_map: Dict[Tuple[str, str], float] = {}
    for row in hist.itertuples(index=False):
        price_map[(str(row.Date)[:10], str(row.Ticker).upper())] = float(row.AdjClose)

    # ── Holdings snapshot for portfolio value approximation ───────────────────
    fixed_prices = (policy.get("governance", {}).get("fixed_asset_prices", {}) or {})
    hold_rows: List[Tuple[str, float]] = []
    for _, r in hold.iterrows():
        t = str(r.get("Ticker", "")).strip().upper()
        try:
            shares = float(r.get("Shares", 0) or 0)
        except (ValueError, TypeError):
            shares = 0.0
        if t and shares:
            hold_rows.append((t, shares))

    def _compute_pv(date_str: str, override: Optional[float] = None) -> Optional[float]:
        if override is not None:
            return override
        total = 0.0
        for t, shares in hold_rows:
            fe = fixed_prices.get(t)
            if fe is not None:
                if isinstance(fe, dict):
                    if str(fe.get("price_type", "")).lower() == "market":
                        px = price_map.get((date_str, t)) or float(fe.get("fallback_price") or 0)
                    else:
                        px = float(fe.get("fallback_price") or 0)
                else:
                    px = float(fe)
            else:
                px = price_map.get((date_str, t), 0.0)
            total += shares * px
        return total if total > 0 else None

    # ── Column header (must match GAS buildPerfLogHeader_) ───────────────────
    header = (
        ["Date", "PortfolioValue", "CashFlow"]
        + [f"Price_{b}" for b in benches]
        + ["PortfolioPct"]
        + [f"Pct_{b}" for b in benches]
        + [f"Diff_{b}" for b in benches]
        + ["EventLabel"]
    )

    # ── Load existing log ─────────────────────────────────────────────────────
    if os.path.exists(perf_log):
        try:
            existing_df = pd.read_csv(perf_log, dtype=str)
            existing_df.columns = [c.strip() for c in existing_df.columns]
        except Exception as e:
            logger.warning("update_performance_log: cannot read existing log (%s); starting fresh", e)
            existing_df = pd.DataFrame(columns=header)
    else:
        existing_df = pd.DataFrame(columns=header)

    for col in header:
        if col not in existing_df.columns:
            existing_df[col] = ""

    existing_by_date: Dict[str, Dict[str, str]] = {
        str(r["Date"]).strip(): dict(r)
        for _, r in existing_df.iterrows()
        if str(r.get("Date", "")).strip()
    }

    # ── Date universe: all trading days in hist >= chart_start ───────────────
    today_str = datetime.today().strftime("%Y-%m-%d")
    recompute_cutoff = (datetime.today() - timedelta(days=5)).strftime("%Y-%m-%d")

    all_trade_dates = sorted({
        str(d)[:10]
        for d in hist["Date"].unique()
        if str(d)[:10] >= chart_start
    })
    if not all_trade_dates:
        logger.warning("update_performance_log: no hist dates >= chart_start %s — skipping", chart_start)
        return

    # ── Build merged row list ─────────────────────────────────────────────────
    rows: List[Dict[str, str]] = []
    for date_str in all_trade_dates:
        ex = existing_by_date.get(date_str, {})
        in_window = date_str >= recompute_cutoff
        is_today  = (date_str == today_str)

        # Portfolio value
        if is_today and today_total_val is not None:
            pv_str = f"{today_total_val:.2f}"
        elif ex.get("PortfolioValue", "") and not in_window:
            pv_str = ex["PortfolioValue"]
        else:
            pv = _compute_pv(date_str, today_total_val if is_today else None)
            pv_str = f"{pv:.2f}" if pv is not None else ex.get("PortfolioValue", "")

        # Benchmark prices (refresh from hist; fall back to existing)
        bench_px: Dict[str, str] = {}
        for b in benches:
            bp = price_map.get((date_str, b))
            bench_px[b] = f"{bp:.2f}" if bp is not None else ex.get(f"Price_{b}", "")

        # Cash flow: preserve non-zero manual entries; else derive from policy
        existing_cf_str = ex.get("CashFlow", "").strip()
        try:
            cf_existing = float(existing_cf_str) if existing_cf_str else None
        except (ValueError, TypeError):
            cf_existing = None
        cf_val = cf_existing if (cf_existing is not None and cf_existing != 0.0) \
                 else _get_scheduled_cash_flow(date_str, policy)

        # EventLabel: preserve existing; auto-fill for known scheduled flows
        event_label = str(ex.get("EventLabel", "")).strip()
        if not event_label and cf_val != 0.0:
            for flow in (policy.get("scheduled_cash_flows") or []):
                if _get_scheduled_cash_flow(date_str, {"scheduled_cash_flows": [flow]}) != 0.0:
                    lbl = str(flow.get("label", "Scheduled flow")).strip()
                    amt = abs(int(flow.get("amount", 0)))
                    event_label = f"{lbl} {amt}"
                    break

        row: Dict[str, str] = {
            "Date":           date_str,
            "PortfolioValue": pv_str,
            "CashFlow":       f"{cf_val:.2f}",
            "PortfolioPct":   ex.get("PortfolioPct", "") if not in_window else "",
            "EventLabel":     event_label,
        }
        for b in benches:
            row[f"Price_{b}"] = bench_px.get(b, "")
            row[f"Pct_{b}"]   = ex.get(f"Pct_{b}", "")  if not in_window else ""
            row[f"Diff_{b}"]  = ex.get(f"Diff_{b}", "") if not in_window else ""
        rows.append(row)

    result_df = pd.DataFrame(rows, columns=header)

    # ── Baseline index and benchmark base prices ──────────────────────────────
    base_idx_list = result_df.index[result_df["Date"] == chart_start].tolist()
    base_idx = base_idx_list[0] if base_idx_list else 0

    base_bench: Dict[str, Optional[float]] = {}
    for b in benches:
        try:
            base_bench[b] = float(result_df.at[base_idx, f"Price_{b}"])
        except (ValueError, TypeError):
            base_bench[b] = None

    # ── TWR chain recompute ───────────────────────────────────────────────────
    # Outside the window: carry forward prev_pv / prev_cum as anchor for the chain.
    # Inside the window: full recompute using the preserved anchor.
    prev_pv:  Optional[float] = None
    prev_cum: Optional[float] = None

    for i in result_df.index:
        date_str = result_df.at[i, "Date"]
        in_window = date_str >= recompute_cutoff

        # Refresh benchmark pct inside window (price revisions may have changed prices)
        if in_window:
            for b in benches:
                if base_bench.get(b):
                    try:
                        bp = float(result_df.at[i, f"Price_{b}"])
                        result_df.at[i, f"Pct_{b}"] = f"{(bp / base_bench[b]) - 1:.4f}"
                    except (ValueError, TypeError):
                        pass

        try:
            pv = float(result_df.at[i, "PortfolioValue"])
        except (ValueError, TypeError):
            continue

        if not in_window:
            # Outside window — update anchor and move on
            try:
                prev_cum = float(result_df.at[i, "PortfolioPct"])
                prev_pv  = pv
            except (ValueError, TypeError):
                pass
            continue

        # Inside window — compute TWR
        if i == base_idx:
            cum = 0.0
        elif prev_pv is not None and prev_cum is not None:
            try:
                cf = float(result_df.at[i, "CashFlow"])
            except (ValueError, TypeError):
                cf = 0.0
            adj_prev = prev_pv + cf
            if adj_prev > 0:
                cum = (1.0 + prev_cum) * (1.0 + (pv / adj_prev) - 1.0) - 1.0
            else:
                cum = prev_cum
        else:
            # No anchor yet (row is before first computable point)
            prev_pv  = pv
            prev_cum = 0.0
            continue

        result_df.at[i, "PortfolioPct"] = f"{cum:.4f}"
        for b in benches:
            try:
                pct_b = float(result_df.at[i, f"Pct_{b}"])
                result_df.at[i, f"Diff_{b}"] = f"{cum - pct_b:.4f}"
            except (ValueError, TypeError):
                pass

        prev_pv  = pv
        prev_cum = cum

    result_df.to_csv(perf_log, index=False)
    logger.info(
        "Performance log updated → %s  (%d rows, through %s)",
        perf_log, len(result_df), all_trade_dates[-1],
    )


# ==============================================================================
# 6) Drawdown enforcement gate
# ==============================================================================

def check_drawdown_state(policy: dict, perf_log: str = PERF_LOG_CSV) -> dict:
    """
    Read the performance log and return the current drawdown enforcement state.

    Returns a dict with keys:
        state     : "normal" | "soft_limit" | "hard_limit"
        drawdown  : float  — current peak-to-trough as a negative fraction (e.g. -0.23)
        soft_limit: float  — policy soft-limit threshold (e.g. 0.20)
        hard_limit: float  — policy hard-limit threshold (e.g. 0.28)
        recovery  : float  — recovery threshold (e.g. 0.12)
    """
    risk  = policy.get("risk_controls", {}) or {}
    soft  = float(risk.get("soft_limit",           0.20))
    hard  = float(risk.get("hard_limit",           0.28))
    recov = float(risk.get("recovery_threshold",   0.12))

    default: dict = {
        "state": "normal", "drawdown": 0.0,
        "soft_limit": soft, "hard_limit": hard, "recovery": recov,
    }

    if not os.path.exists(perf_log):
        return default

    try:
        df = pd.read_csv(perf_log, dtype=str)
        df.columns = [c.strip() for c in df.columns]
        twr_col = next((c for c in df.columns if "twr" in c.lower()), None)
        if twr_col is None:
            return default

        series = pd.to_numeric(df[twr_col], errors="coerce").dropna()
        if series.empty:
            return default

        # Build wealth index from cumulative daily returns, then measure max drawdown
        wealth = (1 + series).cumprod()
        dd = _compute_max_drawdown(wealth)   # returns negative float
        if dd is None:
            return default

        abs_dd = abs(dd)
        if abs_dd >= hard:
            state = "hard_limit"
        elif abs_dd >= soft:
            state = "soft_limit"
        else:
            state = "normal"

        return {"state": state, "drawdown": dd,
                "soft_limit": soft, "hard_limit": hard, "recovery": recov}

    except Exception as exc:
        logger.warning("check_drawdown_state: could not parse perf log (%s); assuming normal", exc)
        return default


# ==============================================================================
# 7) Main
# ==============================================================================
# 7) Execution gate (v2.9.4) — per-ticker z-score timing filter with vol clamp
# ==============================================================================

def compute_ewma_vol_2d(prices: pd.Series, span: int = 126) -> Optional[float]:
    """
    Compute EWMA-based annualised vol from a price series and convert to 2-day vol.
      vol_2d = vol_ann / sqrt(126)   [equivalent to daily_vol * sqrt(2)]
    Returns None if there are fewer than span // 2 observations available.
    """
    if len(prices) < span // 2:
        return None
    log_rets = np.log(prices / prices.shift(1)).dropna()
    ewma_var = log_rets.ewm(span=span, adjust=False).var()
    if ewma_var.empty or pd.isna(ewma_var.iloc[-1]):
        return None
    vol_ann = float(np.sqrt(ewma_var.iloc[-1] * 252))
    return vol_ann / float(np.sqrt(126))


def compute_rv1y_2d(prices: pd.Series, window: int = 252) -> Optional[float]:
    """
    Compute 1-year realised vol (252-day rolling std of daily log returns, annualised)
    and convert to 2-day vol units to match compute_ewma_vol_2d output.
      rv1y_2d = (rolling_std(log_rets, 252) * sqrt(252)) / sqrt(126)
    Returns None if fewer than window // 2 observations are available.
    Used by vol clamp (v2.9.4): effective_vol = clamp(ewma_vol, 0.75×rv1y, 1.50×rv1y).
    """
    if len(prices) < window // 2:
        return None
    log_rets = np.log(prices / prices.shift(1)).dropna()
    if len(log_rets) < window // 2:
        return None
    rv_ann = log_rets.rolling(window, min_periods=window // 2).std().iloc[-1] * np.sqrt(252)
    if pd.isna(rv_ann) or rv_ann <= 0:
        return None
    return float(rv_ann) / float(np.sqrt(126))


def check_execution_gate(
    policy: dict,
    ticker: str,
    trade_direction: str,   # "BUY" or "SELL"
    hist: pd.DataFrame,
    stress_active: bool = False,
) -> dict:
    """
    Evaluate the v2.9.4 execution gate for a single ticker + trade direction.

    Policy logic (mws_policy.json § execution_gates.short_term_confirmation):
      - BUY  defer:    z >= +buy_sigma   → defer up to max_defer_calendar_days
      - SELL defer:    z <= -sell_sigma  → defer up to max_defer_calendar_days
      - SELL spike-trim: z >= +2.0σ AND direction=SELL → execute immediately
      - Stress regime: sell-defer window collapses to 3 calendar days

    v2.9.4 change: vol clamp applied before z-score.
      effective_vol = clamp(ewma_vol, 0.75×RV1y, 1.50×RV1y)
      z = ret_2d / effective_vol
    Clamp is enabled by vol_clamp_enabled in short_term_confirmation.
    Falls back to raw EWMA vol if RV1y cannot be computed (insufficient history).

    v2.9.3 change: per-ticker gate_sigma_buy / gate_sigma_sell overrides in
    execution_gates.per_ticker_thresholds take precedence over global
    buy_defer_sigma / sell_defer_sigma for fat_tail_fixed tickers.

    Returns dict with keys:
      action           : "proceed" | "defer" | "spike_trim"
      reason           : human-readable explanation
      z_score          : float | None
      threshold        : float | None  (unsigned magnitude)
      sigma_used       : float
      gate_source      : "per_ticker_override" | "global_default"
      max_defer_days   : int
      vol_clamp_type   : "floor" | "ceiling" | "none" | "n/a"
      raw_vol_2d       : float | None
      effective_vol_2d : float | None
    """
    gate_cfg = (
        policy.get("execution_gates", {})
              .get("short_term_confirmation", {})
    )
    if not gate_cfg.get("enabled", True):
        return {"action": "proceed", "reason": "gate_disabled",
                "z_score": None, "threshold": None,
                "sigma_used": 0.0, "gate_source": "n/a", "max_defer_days": 0,
                "vol_clamp_type": "n/a", "raw_vol_2d": None, "effective_vol_2d": None}

    direction = trade_direction.upper()

    # ── Global sigma defaults ─────────────────────────────────────────────────
    global_buy_sigma  = float(gate_cfg.get("buy_defer_sigma",  2.0))
    global_sell_sigma = float(gate_cfg.get("sell_defer_sigma", 2.5))
    max_defer_days    = int(gate_cfg.get("max_defer_calendar_days", 10))

    # Stress: sell-defer window collapses (buy-defer unchanged)
    if stress_active and direction == "SELL":
        stress_ovr = gate_cfg.get("stress_regime_overrides", {})
        max_defer_days = int(stress_ovr.get("sell_defer_max_calendar_days", 3))

    # ── Per-ticker sigma overrides (v2.9.3) ───────────────────────────────────
    per_ticker = (
        policy.get("execution_gates", {})
              .get("per_ticker_thresholds", {})
              .get(ticker, {})
    )
    if direction == "BUY":
        sigma      = float(per_ticker["gate_sigma_buy"]) \
                     if "gate_sigma_buy"  in per_ticker else global_buy_sigma
        gate_source = "per_ticker_override" if "gate_sigma_buy"  in per_ticker \
                      else "global_default"
    else:
        sigma      = float(per_ticker["gate_sigma_sell"]) \
                     if "gate_sigma_sell" in per_ticker else global_sell_sigma
        gate_source = "per_ticker_override" if "gate_sigma_sell" in per_ticker \
                      else "global_default"

    # ── Compute EWMA vol_2d from price history ────────────────────────────────
    span   = int(gate_cfg.get("ewma_span_days", 126))
    t_data = hist[hist["Ticker"] == ticker].sort_values("Date")
    if t_data.empty:
        return {"action": "proceed",
                "reason": f"no_price_history_for_{ticker}",
                "z_score": None, "threshold": None,
                "sigma_used": sigma, "gate_source": gate_source,
                "max_defer_days": max_defer_days,
                "vol_clamp_type": "n/a", "raw_vol_2d": None, "effective_vol_2d": None}

    prices = t_data.set_index("Date")["AdjClose"]
    vol_2d = compute_ewma_vol_2d(prices, span=span)
    if vol_2d is None or vol_2d == 0:
        return {"action": "proceed",
                "reason": "insufficient_vol_history",
                "z_score": None, "threshold": None,
                "sigma_used": sigma, "gate_source": gate_source,
                "max_defer_days": max_defer_days,
                "vol_clamp_type": "n/a", "raw_vol_2d": None, "effective_vol_2d": None}

    # ── Vol clamp (v2.9.4): effective_vol = clamp(ewma_vol, 0.75×RV1y, 1.50×RV1y) ──
    clamp_enabled   = gate_cfg.get("vol_clamp_enabled", False)
    effective_vol_2d = vol_2d
    vol_clamp_type   = "none"
    if clamp_enabled:
        rv1y_window = int(gate_cfg.get("vol_clamp_rv1y_window_days", 252))
        floor_mult  = float(gate_cfg.get("vol_clamp_floor_multiplier",  0.75))
        ceil_mult   = float(gate_cfg.get("vol_clamp_ceiling_multiplier", 1.50))
        rv1y_2d = compute_rv1y_2d(prices, window=rv1y_window)
        if rv1y_2d is not None and rv1y_2d > 0:
            floor_2d = rv1y_2d * floor_mult
            ceil_2d  = rv1y_2d * ceil_mult
            if vol_2d < floor_2d:
                effective_vol_2d = floor_2d
                vol_clamp_type   = "floor"
            elif vol_2d > ceil_2d:
                effective_vol_2d = ceil_2d
                vol_clamp_type   = "ceiling"

    # ── 2-day return (latest close vs close 2 trading days prior) ────────────
    if len(prices) < 3:
        return {"action": "proceed",
                "reason": "insufficient_ret2d_history",
                "z_score": None, "threshold": None,
                "sigma_used": sigma, "gate_source": gate_source,
                "max_defer_days": max_defer_days,
                "vol_clamp_type": vol_clamp_type,
                "raw_vol_2d": vol_2d, "effective_vol_2d": effective_vol_2d}

    ret_2d = (prices.iloc[-1] / prices.iloc[-3]) - 1.0

    # ── Z-score and gate decision ─────────────────────────────────────────────
    z         = ret_2d / effective_vol_2d
    threshold = sigma * effective_vol_2d   # unsigned magnitude for logging

    if direction == "SELL" and z >= global_buy_sigma:
        # Spike-trim: large upward move on a sell → execute immediately into strength
        return {"action": "spike_trim",
                "reason": (f"spike_trim: z={z:.2f} >= +{global_buy_sigma}σ on SELL "
                           f"(execute into strength)"),
                "z_score": z, "threshold": global_buy_sigma * effective_vol_2d,
                "sigma_used": global_buy_sigma, "gate_source": "global_default",
                "max_defer_days": 0,
                "vol_clamp_type": vol_clamp_type,
                "raw_vol_2d": vol_2d, "effective_vol_2d": effective_vol_2d}

    if direction == "BUY" and z >= sigma:
        return {"action": "defer",
                "reason": (f"buy_defer: z={z:.2f} >= +{sigma}σ "
                           f"[{gate_source}] — don't chase spike"),
                "z_score": z, "threshold": threshold,
                "sigma_used": sigma, "gate_source": gate_source,
                "max_defer_days": max_defer_days,
                "vol_clamp_type": vol_clamp_type,
                "raw_vol_2d": vol_2d, "effective_vol_2d": effective_vol_2d}

    if direction == "SELL" and z <= -sigma:
        return {"action": "defer",
                "reason": (f"sell_defer: z={z:.2f} <= -{sigma}σ "
                           f"[{gate_source}] — don't sell into capitulation"),
                "z_score": z, "threshold": -threshold,
                "sigma_used": sigma, "gate_source": gate_source,
                "max_defer_days": max_defer_days,
                "vol_clamp_type": vol_clamp_type,
                "raw_vol_2d": vol_2d, "effective_vol_2d": effective_vol_2d}

    return {"action": "proceed",
            "reason": (f"z={z:.2f}, ±{sigma}σ threshold [{gate_source}], "
                       f"within normal range"),
            "z_score": z, "threshold": threshold,
            "sigma_used": sigma, "gate_source": gate_source,
            "max_defer_days": max_defer_days,
            "vol_clamp_type": vol_clamp_type,
            "raw_vol_2d": vol_2d, "effective_vol_2d": effective_vol_2d}


# ==============================================================================
def compute_and_persist_breadth_states(
    policy: dict,
    df_scores: pd.DataFrame,
    state_path: str = BREADTH_STATE_JSON,
) -> dict:
    """Compute per-sleeve breadth state with hysteresis and persist to JSON.

    For every L2 sleeve whose floor is a breadth_conditioned object (v2.9.6),
    this function:
      1. Counts tickers with RawScore > 0 today.
      2. Loads the existing state (consecutive-day counters) from state_path.
      3. Applies hysteresis: the floor state only transitions after the new
         breadth condition has held for `hysteresis_days` consecutive trading days.
      4. Writes the updated state back to state_path.
      5. Returns a dict mapping sleeve_name → effective_floor (float).

    Called from main() after generate_rankings().  The runner reads state_path
    at report time so it uses the fully hysteresis-resolved floor value.
    """
    today_str = _todays_trading_date()
    sleeves_l2 = (policy.get("sleeves", {}) or {}).get("level2", {})

    # Load existing state (keyed by sleeve name)
    existing: dict = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception as exc:
            logger.warning("breadth_state: could not load %s (%s); starting fresh", state_path, exc)

    # Build RawScore lookup
    raw_by_ticker: dict = {}
    if not df_scores.empty and "RawScore" in df_scores.columns:
        for _, row in df_scores.iterrows():
            t = str(row["Ticker"])
            raw = row["RawScore"]
            raw_by_ticker[t] = float(raw) if pd.notna(raw) else None

    effective_floors: dict = {}

    for sleeve_name, l2_data in sleeves_l2.items():
        floor_def = l2_data.get("floor")
        if not isinstance(floor_def, dict) or floor_def.get("type") != "breadth_conditioned":
            continue  # static floor — no state tracking needed

        tickers       = l2_data.get("tickers", [])
        cond          = floor_def.get("breadth_condition", {})
        strong_thresh = int(cond.get("strong_breadth_threshold", 3))
        hyst_days     = int(cond.get("hysteresis_days", 5))
        strong_floor  = float(floor_def.get("strong_breadth_floor", 0.22))
        weak_floor    = float(floor_def.get("weak_breadth_floor", 0.12))
        infeas_floor  = float(floor_def.get("infeasible_floor", 0.0))
        infeas_cond   = str(floor_def.get("infeasible_condition", ""))

        # Today's breadth counts
        positive_count  = sum(1 for t in tickers if (raw_by_ticker.get(t) or 0.0) > 0)
        floor_exit_count = sum(1 for t in tickers if raw_by_ticker.get(t) is None)  # not ranked = exited

        # Determine today's raw breadth category (before hysteresis)
        infeasible_today = (
            positive_count == 0
            or ("floor_exit_count >= 4" in infeas_cond and floor_exit_count >= 4)
        )
        if infeasible_today:
            raw_category = "infeasible"
        elif positive_count >= strong_thresh:
            raw_category = "strong"
        else:
            raw_category = "weak"

        # Load prior state for this sleeve
        prior = existing.get(sleeve_name, {})
        current_category     = prior.get("current_category", raw_category)
        pending_category     = prior.get("pending_category", raw_category)
        pending_days         = int(prior.get("pending_days", 0))
        last_date            = prior.get("last_date", "")

        # Only advance the counter on a new trading day
        if last_date == today_str:
            # Already updated today — return stored effective floor
            effective_floor = {
                "strong":    strong_floor,
                "weak":      weak_floor,
                "infeasible": infeas_floor,
            }.get(current_category, strong_floor)
            effective_floors[sleeve_name] = effective_floor
            existing[sleeve_name] = prior  # no change
            continue

        # Check if raw category has changed from the pending category
        if raw_category != pending_category:
            # New direction — reset pending counter
            pending_category = raw_category
            pending_days     = 1
        else:
            pending_days += 1

        # Transition if pending has held long enough
        if pending_days >= hyst_days and pending_category != current_category:
            logger.info(
                "breadth_state [%s]: floor transitioning %s → %s "
                "(positive_count=%d, held %d days)",
                sleeve_name, current_category, pending_category,
                positive_count, pending_days,
            )
            current_category = pending_category

        effective_floor = {
            "strong":    strong_floor,
            "weak":      weak_floor,
            "infeasible": infeas_floor,
        }.get(current_category, strong_floor)

        logger.info(
            "breadth_state [%s]: category=%s floor=%.0f%% "
            "(positive=%d/%d, pending=%s×%d)",
            sleeve_name, current_category, effective_floor * 100,
            positive_count, len(tickers), pending_category, pending_days,
        )

        existing[sleeve_name] = {
            "current_category": current_category,
            "pending_category": pending_category,
            "pending_days":     pending_days,
            "last_date":        today_str,
            "positive_count":   positive_count,
            "floor_exit_count": floor_exit_count,
            "effective_floor":  effective_floor,
        }
        effective_floors[sleeve_name] = effective_floor

    # Persist updated state
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception as exc:
        logger.warning("breadth_state: could not write %s (%s)", state_path, exc)

    return effective_floors


def main() -> None:
    policy, hist, hold = load_system_files()

    # ── Drawdown gate — enforced before any ranking or rebalance logic ────────
    dd = check_drawdown_state(policy)
    if dd["state"] == "hard_limit":
        logger.error(
            "HARD LIMIT ACTIVE — drawdown %.1f%% ≥ hard limit %.1f%%. "
            "Reducing all sleeves to policy floors. New buys and rebalances SUSPENDED.",
            abs(dd["drawdown"]) * 100, dd["hard_limit"] * 100,
        )
    elif dd["state"] == "soft_limit":
        logger.warning(
            "SOFT LIMIT ACTIVE — drawdown %.1f%% ≥ soft limit %.1f%%. "
            "New buys SUSPENDED; calendar and signal-drift triggers FROZEN.",
            abs(dd["drawdown"]) * 100, dd["soft_limit"] * 100,
        )
    else:
        logger.info("Drawdown status: normal (current %.1f%%)", abs(dd["drawdown"]) * 100)

    candidates, _, _ = run_mws_audit(policy, hist, hold)
    _ = calculate_portfolio_value(policy, hold, hist)

    df_scores = generate_rankings(policy, hist, candidates, hold)
    compute_and_persist_breadth_states(policy, df_scores)
    from mws_charts import rotate_and_chart   # local import — keeps matplotlib out of CI
    rotate_and_chart(df_scores, policy)

    logger.info("Run complete.")

if __name__ == "__main__":
    main()



