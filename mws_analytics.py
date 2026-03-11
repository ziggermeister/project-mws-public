import json
import logging
import os
import platform
import re
import subprocess
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, List, Set, Any

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# Suppress only known-harmless warnings; do not suppress all warnings globally.
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")
warnings.filterwarnings("ignore", message=".*non-GUI backend.*")

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

POLICY_FILENAME  = "mws_policy.json"
TRACKER_FILENAME = "mws_tracker.json"
HOLDINGS_CSV     = "mws_holdings.csv"
HISTORY_CSV      = "mws_ticker_history.csv"
PERF_LOG_CSV     = "mws_recent_performance.csv"
CHART_FILENAME   = "mws_equity_curve.png"


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

def load_system_files() -> Tuple[dict, dict, pd.DataFrame, pd.DataFrame]:
    required = [POLICY_FILENAME, TRACKER_FILENAME, HOLDINGS_CSV, HISTORY_CSV]
    missing = [f for f in required if not os.path.exists(f)]
    if missing:
        _fatal(f"[FATAL] System halted. Missing files: {missing}")

    logger.info("Phase 0: Loading System Files...")
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
    state  = _load_json(TRACKER_FILENAME)

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

    return policy, state, hist, hold


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
def run_mws_audit(policy: dict, state: dict, hist: pd.DataFrame, hold: pd.DataFrame):
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
        return pd.DataFrame(columns=["Ticker", "Score", "Pct", "Alpha", "AlphaVs", "Sleeve", "Status"])

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
        return pd.DataFrame(columns=["Ticker", "Score", "Pct", "Alpha", "AlphaVs", "Sleeve", "Status"])

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

    return df[["Ticker", "Score", "Pct", "Alpha", "AlphaVs", "Sleeve", "Status"]]


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

def _rolling_alpha(port: pd.Series, bench: pd.Series, window: int = 30) -> pd.Series:
    """30-day trailing return of portfolio minus 30-day trailing return of benchmark."""
    p = pd.to_numeric(port,  errors="coerce")
    b = pd.to_numeric(bench, errors="coerce")
    levels_p = (1 + p)
    levels_b = (1 + b)
    roll_p = levels_p / levels_p.shift(window) - 1
    roll_b = levels_b / levels_b.shift(window) - 1
    return (roll_p - roll_b).where(roll_p.notna() & roll_b.notna())

# ---------------------------------------------------------------------------
# Event label backfill via Claude (no news API needed)
# ---------------------------------------------------------------------------

def _backfill_event_labels(
    csv_path: str,
    port_col: str,
    bench_cols: list,
    date_col: str = "Date",
    move_threshold: float = 0.0175,
    lookback_days: int = 5,
) -> None:
    """
    Backfill EventLabel only for a recent rolling window and update labels by Date.
    Idempotent: never overwrites existing non-empty EventLabel values.
    """
    if not os.path.exists(csv_path):
        return

    df = pd.read_csv(csv_path, dtype=str).fillna("")
    df.columns = [c.strip() for c in df.columns]

    if "EventLabel" not in df.columns:
        df["EventLabel"] = ""

    dc = next((c for c in df.columns if c.lower() == date_col.lower()), None)
    pc = next((c for c in df.columns if c.lower() == port_col.lower()), None)
    if not dc or not pc:
        return

    df[dc] = pd.to_datetime(df[dc], errors="coerce")
    df = df.dropna(subset=[dc]).sort_values(dc).reset_index(drop=True)

    bench_cols = [b for b in bench_cols if b in df.columns]
    numeric_cols = [pc] + bench_cols
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — skipping event label backfill")
        return

    def _api_round_trip(messages):
        body = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 512,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": messages,
        }, ensure_ascii=True).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read())

    def _daily_moves_for_row(i: int) -> dict:
        if i <= 0:
            return {}
        moves = {}
        for c in numeric_cols:
            prev = pd.to_numeric(df.loc[i - 1, c], errors="coerce")
            cur = pd.to_numeric(df.loc[i, c], errors="coerce")
            if pd.notna(prev) and pd.notna(cur) and (1.0 + prev) > 0:
                moves[c] = ((1.0 + cur) / (1.0 + prev)) - 1.0
        return moves

    def _format_moves_for_prompt(moves: dict) -> List[tuple]:
        """Returns list of (display_name, pct_value) for MWS + all bench cols."""
        result = [("MWS", float(moves.get(pc, 0.0)) * 100.0)]
        for c in bench_cols:
            val = float(moves.get(c, 0.0)) * 100.0
            ticker = c.replace("Pct_", "").replace("pct_", "")
            result.append((_bench_display(ticker), val))
        return result

    def _fetch_label_from_anthropic(date_str: str, moves: dict) -> str:
        move_lines = _format_moves_for_prompt(moves)
        moves_text = "\n".join(f"{name} {val:+.2f}%" for name, val in move_lines)
        bench_names = ", ".join(name for name, _ in move_lines[1:])

        user_prompt = f"""You are annotating a US market chart.

DATE: {date_str}

MOVES:
{moves_text}

If max absolute move is below {move_threshold*100:.2f}%, return exactly:
SKIP

Otherwise use web_search to identify the most credible driver for {date_str}.
Prefer Reuters Bloomberg WSJ FT CNBC MarketWatch Barrons Fed BLS BEA.
Do not invent facts.

Return exactly one single line in this format:
<driver <= 7 words> | <series> <+/-X.XX%>

Rules:
- no commas
- no quotes
- no bullets
- no line breaks
- use pipe separator
- maximum 80 characters
- if unknown return:
Unknown | <series> <+/-X.XX%>
"""

        messages = [{"role": "user", "content": user_prompt}]
        for _ in range(8):
            data = _api_round_trip(messages)
            content = data.get("content") or []
            stop = data.get("stop_reason", "")
            tool_uses = [b for b in content if b.get("type") == "tool_use"]

            if not tool_uses or stop == "end_turn":
                text = ""
                for block in reversed(content):
                    if block.get("type") == "text":
                        text = (block.get("text") or "").strip()
                        break
                label = sanitize_event_label(text)
                if label.upper().startswith("SKIP"):
                    label = ""
                return label

            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": ""}
                    for tu in tool_uses
                ],
            })
        return ""

    # recent_dates = recent_calendar_dates(datetime.today().strftime("%Y-%m-%d"), days_back=lookback_days)
    as_of_date_str = df[dc].max().strftime("%Y-%m-%d")
    recent_dates = recent_calendar_dates(as_of_date_str, days_back=lookback_days)

    # recent_set = set(recent_dates)

    def _label_fn(date_str: str) -> str:
        row = df[df[dc].dt.strftime("%Y-%m-%d") == date_str]
        if row.empty:
            return ""
        i = int(row.index[0])
        if i <= 0:
            return ""

        existing = str(df.loc[i, "EventLabel"]).strip()
        if existing and existing.lower() != "nan":
            return existing

        moves = _daily_moves_for_row(i)
        if not moves:
            return ""
        if max(abs(v) for v in moves.values()) < move_threshold:
            return "SKIP"

        for attempt in range(4):
            try:
                logger.info("Fetching event label for %s ...", date_str)
                return _fetch_label_from_anthropic(date_str, moves)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if e.code == 429 and attempt < 3:
                    wait = 20 * (attempt + 1)
                    logger.warning("Rate limited for %s; retrying in %ds ...", date_str, wait)
                    time.sleep(wait)
                else:
                    logger.error("API error %d for %s: %s", e.code, date_str, body[:200])
                    return ""
            except Exception as e:
                logger.error("Event label fetch failed for %s: %s", date_str, e)
                return ""
        return ""

    apply_recent_event_labels(csv_path, recent_dates, _label_fn)
    logger.info("Reviewed event label window: %s", ", ".join(recent_dates))

def _read_chart_events(
    df: pd.DataFrame,
    date_col: str,
    port_col: str = None,
    bench_cols: list = None,
    n_events: int = None,              # ignored
    move_threshold: float = None       # ignored
) -> list:
    """
    CSV is source of truth.

    - Only rows with non-empty EventLabel are eligible.
    - Rank eligible rows by largest absolute *daily* move (t-1 -> t) across portfolio/benchmarks.
    - Greedy select by magnitude with a 5-day minimum spacing.
    - Event coloring is based on dominant *daily* move sign.
    - Chart label includes the *daily* move magnitudes (MWS + benchmarks) on the first line.
    """
    # Resolve columns
    if port_col is None:
        port_col = next((c for c in df.columns if "portfoliopct" in c.lower()), None)
    if bench_cols is None:
        bench_cols = [c for c in df.columns if c.lower().startswith("pct_")]

    label_col = next((c for c in df.columns if c.strip().lower() == "eventlabel"), None)
    if label_col is None or date_col not in df.columns:
        return []

    numeric_cols = [c for c in [port_col] + (bench_cols or []) if c and c in df.columns]
    if not numeric_cols:
        return []

    df2 = df.copy()
    df2[date_col] = pd.to_datetime(df2[date_col], errors="coerce")
    df2 = df2.dropna(subset=[date_col]).sort_values(date_col).reset_index(drop=True)

    # numeric
    for c in numeric_cols:
        df2[c] = pd.to_numeric(df2[c], errors="coerce")

    # daily moves on FULL timeline from cumulative return series
    for c in numeric_cols:
        prev = df2[c].shift(1)
        df2[f"_d_{c}"] = np.where(
            prev.notna() & df2[c].notna() & ((1.0 + prev) > 0),
            ((1.0 + df2[c]) / (1.0 + prev)) - 1.0,
            np.nan
        )

    # score by max abs daily move
    df2["_max_move"] = df2[[f"_d_{c}" for c in numeric_cols]].abs().max(axis=1)

    # require actual EventLabel (CSV truth)
    df2[label_col] = df2[label_col].astype(str).str.strip()
    df2 = df2[(df2[label_col] != "") & (df2[label_col].str.lower() != "nan")]

    # must have a scored move (i.e., not first row of full timeline)
    df2 = df2.dropna(subset=["_max_move"])
    if df2.empty:
        return []

    # rank by magnitude
    df_ranked = df2.sort_values("_max_move", ascending=False)

    # greedy select with 5-day spacing
    min_gap = pd.Timedelta(days=5)
    selected = []

    def _fmt_mv(x: float) -> str:
        return f"{x*100:+.1f}%"

    for _, row in df_ranked.iterrows():
        d = row[date_col]
        if any(abs(d - s[0]) < min_gap for s in selected):
            continue

        raw_lbl = str(row[label_col]).strip()
        if raw_lbl.lower() in ("", "nan"):
            continue
        base_label = raw_lbl.replace(" | ", "\n")

        # Build first-line metrics from DAILY moves (t-1 -> t)
        metric_parts = []

        mws_mv = None
        if port_col and pd.notna(row.get(f"_d_{port_col}")):
            mws_mv = float(row[f"_d_{port_col}"])
            metric_parts.append(f"MWS {_fmt_mv(mws_mv)}")

        bench_moves = []
        for c in (bench_cols or []):
            if not c or c not in df2.columns:
                continue
            dv = row.get(f"_d_{c}")
            if pd.notna(dv):
                name = c.replace("Pct_", "").replace("pct_", "")
                bench_moves.append((name, float(dv)))

        # include up to 2 most relevant benchmark moves by abs magnitude
        bench_moves.sort(key=lambda t: abs(t[1]), reverse=True)
        for name, mv in bench_moves[:2]:
            metric_parts.append(f"{name} {_fmt_mv(mv)}")

        metrics_line = " | ".join(metric_parts)

        # Dominant daily move for coloring: prefer MWS; else largest benchmark move
        if mws_mv is not None and pd.notna(mws_mv):
            dominant_mv = float(mws_mv)
        elif bench_moves:
            dominant_mv = float(bench_moves[0][1])
        else:
            dominant_mv = 0.0

        full_label = f"{metrics_line}\n{base_label}" if metrics_line else base_label

        selected.append((d, full_label, dominant_mv))

    selected.sort(key=lambda x: x[0])
    return selected
    

def rotate_and_chart(df_scores: pd.DataFrame, policy: dict) -> None:
    """
    Prints portfolio alpha and generates a 2-panel chart:
      Panel 1: Titanium (MWS) vs benchmarks — with fill-between, drawdown shading,
               gap arrows, and stats box.
      Panel 2: Cumulative alpha vs benchmarks with fill-between.
    """
    palpha = compute_portfolio_alpha_from_log(policy)
    if palpha:
        bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
        bench_order = [str(x).strip().upper() for x in (bl.get("active_benchmarks") or [])]
        order = bench_order + sorted(k for k in palpha if k not in bench_order)
        parts = [f"{_bench_display(k)} {palpha[k]}" for k in order if k in palpha]
        if parts:
            print("\n📈 PORTFOLIO ALPHA (since chart baseline): " + " | ".join(parts))

    try:
        if not os.path.exists(PERF_LOG_CSV):
            print("⚠️ Charting skipped: Perf log CSV not found.")
            return

        df_log = pd.read_csv(PERF_LOG_CSV)
        if df_log.empty:
            print("⚠️ Charting skipped: Perf log CSV empty.")
            return

        df_log.columns = [c.strip() for c in df_log.columns]
        date_c = _find_col(df_log, ["Date", "date"])
        if not date_c:
            print("⚠️ Charting skipped: Perf log missing Date column.")
            return

        df_log[date_c] = pd.to_datetime(df_log[date_c], errors="coerce")
        df_log = df_log.dropna(subset=[date_c]).sort_values(date_c).drop_duplicates(subset=[date_c], keep="last")

        # Derive benchmark tickers and display names from policy
        bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
        active_benches = [str(x).strip().upper() for x in (bl.get("active_benchmarks") or []) if x]
        if not active_benches:
            print("⚠️ Charting skipped: no active_benchmarks configured in policy.")
            return
        b0 = active_benches[0]
        b1 = active_benches[1] if len(active_benches) > 1 else None
        disp_b0 = _bench_display(b0)
        disp_b1 = _bench_display(b1) if b1 else None

        # Backfill EventLabel in CSV for any significant-move dates missing a headline
        _backfill_bench_cols = [f"Pct_{b0}"] + ([f"Pct_{b1}"] if b1 else [])
        _backfill_event_labels(
            PERF_LOG_CSV,
            port_col="PortfolioPct",
            bench_cols=_backfill_bench_cols,
            move_threshold=0.0175,
            lookback_days=5,
        )
        # Reload so the freshly-written EventLabels are available for plotting
        df_log = pd.read_csv(PERF_LOG_CSV)
        df_log.columns = [c.strip() for c in df_log.columns]
        df_log[date_c] = pd.to_datetime(df_log[date_c], errors="coerce")
        df_log = df_log.dropna(subset=[date_c]).sort_values(date_c).drop_duplicates(subset=[date_c], keep="last")

        chart_start_str = str(bl.get("chart_start_date") or "").strip()
        if not chart_start_str:
            print("[WARN] policy.governance.reporting_baselines.chart_start_date not set; "
                  "charting will use earliest available data.")
        chart_start = pd.to_datetime(chart_start_str, errors="coerce") if chart_start_str else pd.NaT

        port_col = _find_col(df_log, ["PortfolioPct", "portfoliopct"])
        if not port_col:
            print("⚠️ Charting skipped: Perf log missing PortfolioPct column.")
            return

        df_plot = df_log.copy()
        if pd.notna(chart_start):
            df_plot = df_plot[df_plot[date_c] >= chart_start].copy()

        df_plot[port_col] = pd.to_numeric(df_plot[port_col], errors="coerce")
        df_plot = df_plot.dropna(subset=[port_col])

        title_suffix = f"Since {chart_start.strftime('%b %d, %Y') if pd.notna(chart_start) else chart_start_str}"
        if df_plot.empty:
            print(f"⚠️ No data after {chart_start_str}. Falling back to last 90 observations.")
            df_plot = df_log.tail(90).copy()
            df_plot[port_col] = pd.to_numeric(df_plot[port_col], errors="coerce")
            df_plot = df_plot.dropna(subset=[port_col])
            title_suffix = "Last 90 Days (Fallback)"

        if df_plot.empty:
            print("⚠️ Charting skipped: Perf log has no plottable rows.")
            return

        pct_b0 = _find_col(df_plot, [f"Pct_{b0}", f"pct_{b0.lower()}"])
        pct_b1 = _find_col(df_plot, [f"Pct_{b1}", f"pct_{b1.lower()}"])
        if not pct_b0:
            print(f"⚠️ Benchmark series Pct_{b0} not found; skipping {disp_b0} line.")
        if not pct_b1:
            print(f"⚠️ Benchmark series Pct_{b1} not found; skipping {disp_b1} line.")

        to_num = lambda col: pd.to_numeric(df_plot[col], errors="coerce")
        port_series = to_num(port_col)
        alpha_b0 = (port_series - to_num(pct_b0)) if pct_b0 else None
        alpha_b1 = (port_series - to_num(pct_b1)) if pct_b1 else None

        # ── Drawdown policy thresholds from policy ──────────────────────────────
        dr = policy.get("drawdown_rules", {}) or {}
        soft_limit = float(dr.get("soft_limit", 0.20))
        hard_limit = float(dr.get("hard_limit", 0.28))

        # ── Drawdown series for shading ──────────────────────────────────────────
        levels       = (1 + port_series)
        rolling_peak = levels.cummax()
        drawdown_ser = (levels - rolling_peak) / rolling_peak   # 0 to negative



        # ── Stats for inset box ──────────────────────────────────────────────────
        total_ret  = float(port_series.dropna().iloc[-1])
        max_dd     = _compute_max_drawdown(port_series)
        n_days     = len(port_series.dropna())
        cagr       = (1 + total_ret) ** (252 / max(n_days, 1)) - 1 if n_days >= 10 else None

        # ── Style ────────────────────────────────────────────────────────────────
        try:
            plt.style.use("seaborn-v0_8-whitegrid")
        except Exception:
            plt.style.use("seaborn-whitegrid")

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(22, 11), sharex=True,
            gridspec_kw={"height_ratios": [2, 1]}
        )
        fig.patch.set_facecolor("#f8f9fa")
        ax1.set_facecolor("#f8f9fa")
        ax2.set_facecolor("#f8f9fa")

        dates = df_plot[date_c]

        # ── Helpers ──────────────────────────────────────────────────────────────
        def _pct_color(v: float) -> str:
            """Green for positive, red for negative, grey for zero."""
            if v > 0:   return "#27ae60"
            if v < 0:   return "#e74c3c"
            return "#555555"

        def _label_last(ax, x_ser, y_ser, text, y_nudge_pts=0, val=None):
            if x_ser is None or y_ser is None or len(x_ser) == 0 or len(y_ser) == 0:
                return
            x_last, y_last = x_ser.iloc[-1], float(y_ser.iloc[-1])
            ax.scatter(x_last, y_last, s=80, zorder=6, color="white", edgecolor="#333", linewidth=1.2)
            ax.annotate(
                text, xy=(x_last, y_last), xytext=(10, y_nudge_pts),
                textcoords="offset points", ha="left", va="center",
                fontsize=10, fontweight="bold", color="#222", clip_on=False,
                bbox=dict(boxstyle="square,pad=0.2", fc="white", ec="none", alpha=0.7)
            )

        def _apply_labels(ax, series):
            last_vals = []
            for name, xs, ys in series:
                ys2 = pd.to_numeric(ys, errors="coerce").dropna()
                if ys2.empty:
                    continue
                last_vals.append((name, df_plot.loc[ys2.index, date_c], ys2, float(ys2.iloc[-1])))
            last_vals.sort(key=lambda x: x[3])
            nudges = {name: 0 for name, *_ in last_vals}
            for i in range(1, len(last_vals)):
                prev, cur = last_vals[i - 1], last_vals[i]
                if abs(cur[3] - prev[3]) < 0.003:
                    nudges[cur[0]] = nudges[prev[0]] + 13
            for name, xs, ys, v in last_vals:
                _label_last(ax, xs, ys, f"{name} {v*100:+.2f}%", y_nudge_pts=nudges.get(name, 0), val=v)

        def _mark_extremes(ax, xs, ys, color, max_va="bottom", min_va="top"):
            """Add a dot + Max/Min label at the highest and lowest points of a series.
            max_va/min_va: vertical anchor for the label — 'top' places label below dot,
            'bottom' places label above dot.
            """
            ys2 = pd.to_numeric(ys, errors="coerce").dropna()
            if ys2.empty or len(ys2) < 3:
                return
            xs2 = xs.loc[ys2.index] if hasattr(xs, "loc") else xs

            i_max = int(ys2.idxmax())
            i_min = int(ys2.idxmin())
            if i_max == ys2.index[0] or i_max == ys2.index[-1]:
                i_max = None
            if i_min == ys2.index[0] or i_min == ys2.index[-1]:
                i_min = None

            for idx, prefix, va in [
                (i_max, "Max", max_va),
                (i_min, "Min", min_va),
            ]:
                if idx is None:
                    continue
                xv = xs2.loc[idx]
                yv = float(ys2.loc[idx])
                yo = 10 if va == "bottom" else -10   # "bottom" anchor → label above dot
                ax.scatter(xv, yv, s=80, zorder=7, color=color, edgecolor="#333",
                           linewidth=1.2)
                ax.annotate(
                    f"{prefix}: {yv*100:+.2f}%",
                    xy=(xv, yv), xytext=(0, yo), textcoords="offset points",
                    ha="center", va=va, fontsize=10, fontweight="bold",
                    color="#222", clip_on=False,
                    bbox=dict(boxstyle="square,pad=0.2", fc="white", ec="none", alpha=0.7)
                )

        # ── Panel 1: fill under each line to the true minimum ──────────────────
        _all_vals = pd.concat([port_series] +
                              ([to_num(pct_b0)] if pct_b0 else []) +
                              ([to_num(pct_b1)] if pct_b1 else []),
                              axis=0).dropna()
        _floor = float(_all_vals.min())   # exact lowest point across all series
        if pct_b1:
            b1_s = to_num(pct_b1)
            ax1.fill_between(dates, b1_s, _floor,
                             alpha=0.13, color="#2ca02c",
                             interpolate=True, label="_nolegend_")
        if pct_b0:
            b0_s = to_num(pct_b0)
            ax1.fill_between(dates, b0_s, _floor,
                             alpha=0.13, color="orange",
                             interpolate=True, label="_nolegend_")
        ax1.fill_between(dates, port_series, _floor,
                         alpha=0.18, color="#1f77b4",
                         interpolate=True, label="_nolegend_")

        # ── Panel 1: main lines ──────────────────────────────────────────────────
        if pct_b0:
            ax1.plot(dates, to_num(pct_b0), label=f"{b0} ({disp_b0})", linewidth=1.8,
                     color="orange", alpha=0.85, zorder=3)
        if pct_b1:
            ax1.plot(dates, to_num(pct_b1), label=f"{b1} ({disp_b1})", linewidth=1.8,
                     color="#2ca02c", alpha=0.85, zorder=3)
        ax1.plot(dates, port_series, label="Titanium (MWS)", linewidth=1.8,
                 color="#1f77b4", zorder=4)

        # ── Panel 1: MWS average line ────────────────────────────────────────────
        mws_avg = float(port_series.dropna().mean())
        ax1.axhline(mws_avg, color="#1f77b4", linewidth=1.4, linestyle=(0, (4, 3)),
                    alpha=0.7, zorder=2)


        ax1.set_title(f"Titanium Performance ({title_suffix})  ·  TWR net of cash flows", fontsize=11, fontweight="bold", pad=10)
        ax1.grid(True, alpha=0.45, color="#999999")
        # Build legend from only the three named lines (benchmarks + Titanium)
        # Legend: 3 lines only, top-left above the stats box
        _handles, _labels = ax1.get_legend_handles_labels()
        _legend_items = [(h, l) for h, l in zip(_handles, _labels) if not l.startswith("_")]
        if _legend_items:
            _h, _l = zip(*_legend_items)
            ax1.legend(_h, _l, loc="upper left", fontsize=9, framealpha=0.95,
                       edgecolor="#aaaaaa", fancybox=True,
                       facecolor="white", labelcolor="#222222")
        ax1.yaxis.set_major_formatter(lambda x, pos: f"{x*100:.0f}%")
        # Mark floor on y-axis
        ax1.axhline(_floor, color="#888888", linewidth=0.6, linestyle=":",
                    alpha=0.7, zorder=1)
        _cur_ticks = list(ax1.get_yticks())
        ax1.set_yticks(sorted(set(_cur_ticks + [_floor])))
        ax1.yaxis.set_major_formatter(lambda x, pos: f"{x*100:.0f}%")

        # ── Panel 1: right-side end labels ──────────────────────────────────────
        series1 = [("Titanium MWS:", dates, port_series)]
        if pct_b0: series1.append((f"{disp_b0}:", dates, to_num(pct_b0)))
        if pct_b1: series1.append((f"{disp_b1}:", dates, to_num(pct_b1)))
        _apply_labels(ax1, series1)
        _mark_extremes(ax1, dates.reset_index(drop=True), port_series.reset_index(drop=True), "#1f77b4")
        if pct_b0: _mark_extremes(ax1, dates.reset_index(drop=True), to_num(pct_b0).reset_index(drop=True), "orange", max_va="bottom", min_va="top")
        if pct_b1: _mark_extremes(ax1, dates.reset_index(drop=True), to_num(pct_b1).reset_index(drop=True), "#2ca02c", max_va="bottom", min_va="bottom")

        # ── Panel 1: horizontal reference lines at last-day value ────────────────
        line_styles = [
            (port_series,                              "#1f77b4", 0.8),
            (to_num(pct_b0) if pct_b0 else None,      "orange",  0.8),
            (to_num(pct_b1) if pct_b1 else None,      "#2ca02c", 0.8),
        ]
        x_start = dates.iloc[0]
        for ys, color, lw in line_styles:
            if ys is None:
                continue
            ys2 = pd.to_numeric(ys, errors="coerce").dropna()
            if ys2.empty:
                continue
            y_end = float(ys2.iloc[-1])
            ax1.axhline(y_end, color=color, linewidth=lw * 2, linestyle="--",
                        alpha=0.5, zorder=1)


        # ── Panel 1: MWS average line — dash-dot, lighter blue, distinct from dashed last-value lines
        port_clean = pd.to_numeric(port_series, errors="coerce").dropna()
        if not port_clean.empty:
            port_avg = float(port_clean.mean())
            ax1.axhline(port_avg, color="#333333", linewidth=1.4, linestyle="-",
                        alpha=0.5, zorder=2)
            ax1.scatter(dates.iloc[-1], port_avg, s=80, zorder=6,
                        color="white", edgecolor="#333333", linewidth=1.2)
            _label_last(ax1, dates, pd.Series([port_avg]*len(dates), index=dates.index),
                        f"Titanium MWS Ave: {port_avg*100:+.2f}%", val=port_avg)

        # ── Panel 2: cumulative alpha with fill-between ──────────────────────────
        ax2.axhline(0, color="#888", linewidth=1.0, zorder=1)

        if alpha_b0 is not None:
            ax2.plot(dates, alpha_b0, linewidth=2.0, color="orange",
                     label=f"vs {disp_b0}", zorder=3)
            ax2.fill_between(dates, alpha_b0, 0,
                             where=(alpha_b0 >= 0), alpha=0.18, color="orange",
                             interpolate=True, label="_nolegend_")
            ax2.fill_between(dates, alpha_b0, 0,
                             where=(alpha_b0 < 0),  alpha=0.18, color="red",
                             interpolate=True, label="_nolegend_")
        if alpha_b1 is not None:
            ax2.plot(dates, alpha_b1, linewidth=2.0, color="#2ca02c",
                     label=f"vs {disp_b1}", zorder=3)
            ax2.fill_between(dates, alpha_b1, 0,
                             where=(alpha_b1 >= 0), alpha=0.18, color="#2ca02c",
                             interpolate=True, label="_nolegend_")
            ax2.fill_between(dates, alpha_b1, 0,
                             where=(alpha_b1 < 0),  alpha=0.18, color="red",
                             interpolate=True, label="_nolegend_")

        ax2.set_title(f"Cumulative Alpha vs. Benchmarks (since {chart_start.strftime('%b %d, %Y') if pd.notna(chart_start) else chart_start_str})",
                      fontsize=11, fontweight="bold", pad=8)
        ax2.grid(True, alpha=0.45, color="#999999")

        _handles2, _labels2 = ax2.get_legend_handles_labels()
        _leg2 = [(h, l) for h, l in zip(_handles2, _labels2) if not l.startswith("_")]
        if _leg2:
            _h2, _l2 = zip(*_leg2)
            ax2.legend(_h2, _l2, loc="upper left", fontsize=9, framealpha=0.95,
                       edgecolor="#aaaaaa", fancybox=True,
                       facecolor="white", labelcolor="#222222")
        ax2.yaxis.set_major_formatter(lambda x, pos: f"{x*100:.0f}%")

        series2 = []
        if alpha_b0 is not None: series2.append((f"vs {disp_b0}:", dates, alpha_b0))
        if alpha_b1 is not None: series2.append((f"vs {disp_b1}:", dates, alpha_b1))
        _apply_labels(ax2, series2)
        if alpha_b0 is not None: _mark_extremes(ax2, dates.reset_index(drop=True), pd.Series(alpha_b0).reset_index(drop=True), "orange", max_va="top", min_va="top")
        if alpha_b1 is not None: _mark_extremes(ax2, dates.reset_index(drop=True), pd.Series(alpha_b1).reset_index(drop=True), "#2ca02c", max_va="bottom", min_va="bottom")

        # ── x-axis: start flush, extend right slightly to show last dot fully ─────
        date_padding = pd.Timedelta(days=max(2, int(
            (dates.iloc[-1] - dates.iloc[0]).days * 0.015)))
        ax1.set_xlim(dates.iloc[0], dates.iloc[-1] + date_padding)

        # ── Weekly vertical reference lines (every Monday) ─────────────────────
        ax2.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %-d"))
        ax2.tick_params(axis="x", which="major", labelsize=8, color="#888",
                        labelcolor="#333", length=4)
        for lbl in ax2.get_xticklabels():
            lbl.set_fontweight("bold")
        for d in pd.date_range(dates.iloc[0], dates.iloc[-1], freq="W-MON"):
            for ax in (ax1, ax2):
                ax.axvline(d, color="#aaaaaa", linewidth=0.6, linestyle="--",
                           alpha=0.4, zorder=1)

        # ── Significant event lines from EventLabel / EventAuto columns ───────────
        chart_events = _read_chart_events(
            df_plot, date_c,
            port_col=port_col,
            bench_cols=[c for c in [pct_b0, pct_b1] if c]
        )

        _total_days = (dates.iloc[-1] - dates.iloc[0]).days or 1
        _label_dates = [d for d, _, _mv in chart_events]
        for _ei, (d, lbl, _dominant_mv) in enumerate(chart_events):
            _evt_color = "#cc2222" if _dominant_mv < 0 else "#1a7a1a"
            for ax in (ax1, ax2):
                ax.axvline(d, color=_evt_color, linewidth=1.2, linestyle="--",
                           alpha=0.6, zorder=3)
            # Align label: right-align for last 20% of window, left for first 20%
            _pos = (d - dates.iloc[0]).days / _total_days
            _ha  = "right" if _pos > 0.92 else ("left" if _pos < 0.08 else "center")
            _xoff = -4 if _ha == "right" else (4 if _ha == "left" else 0)
            # Stagger vertically if a neighbour label is within 12 days
            _neighbours = [abs((d - d2).days) for d2 in _label_dates if d2 != d]
            _yoff = -87 if _neighbours and min(_neighbours) < 12 and _ei % 2 == 1 else -57
            ax2.annotate(
                f"{d.strftime('%A, %b %-d')}\n{lbl}",
                xy=(d, ax2.get_ylim()[0]),
                xytext=(_xoff, _yoff), textcoords="offset points",
                ha=_ha, va="top", fontsize=8.5, fontweight="bold",
                color=_evt_color, clip_on=False
            )

        # Push ax2 y-floor down to give Min labels breathing room above date axis
        #_ax2_ymin, _ax2_ymax = ax2.get_ylim()
        #ax2.set_ylim(_ax2_ymin - abs(_ax2_ymax - _ax2_ymin) * 0.35, _ax2_ymax)
        
        # Tighten ax2 scale: keep at least -1.5% floor, preserve top
        _ax2_ymin, _ax2_ymax = ax2.get_ylim()
        ax2.set_ylim(min(-0.015, _ax2_ymin), _ax2_ymax)

        plt.tight_layout(rect=[0, 0, 0.90, 1])   # leave right margin for labels
        plt.subplots_adjust(bottom=0.28, hspace=0.2)  # extra room for event labels
        plt.savefig(CHART_FILENAME, dpi=150, bbox_inches="tight")
        print(f"\n✅ Chart generated: {CHART_FILENAME}")
        # Auto-open the chart only on macOS; skip silently on Linux/Windows
        if platform.system() == "Darwin":
            subprocess.run(["open", CHART_FILENAME], check=False)

    except Exception as e:
        print(f"⚠️ Charting Error: {e}")
        traceback.print_exc()


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
def main() -> None:
    policy, state, hist, hold = load_system_files()

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

    candidates, _, _ = run_mws_audit(policy, state, hist, hold)
    _ = calculate_portfolio_value(policy, hold, hist)

    df_scores = generate_rankings(policy, hist, candidates, hold)
    rotate_and_chart(df_scores, policy)

    logger.info("Run complete.")

if __name__ == "__main__":
    main()



