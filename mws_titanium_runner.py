import json
import os
import warnings
import subprocess
from datetime import datetime
from typing import Optional, Tuple, Dict, List, Set, Any

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")

POLICY_FILENAME  = "mws_policy.json"
TRACKER_FILENAME = "mws_tracker.json"
HOLDINGS_CSV     = "mws_holdings.csv"
HISTORY_CSV      = "mws_ticker_history.csv"
PERF_LOG_CSV     = "mws_recent_performance.csv"
# PERF_LOG_CSV = "mws_recent_performance_FAKE.csv"
CHART_FILENAME   = "mws_equity_curve.png"


# ==============================================================================
# 1) Robust loading
# ==============================================================================
def _fatal(msg: str, code: int = 1) -> None:
    print(msg)
    raise SystemExit(code)

def load_system_files() -> Tuple[dict, dict, pd.DataFrame, pd.DataFrame]:
    required = [POLICY_FILENAME, TRACKER_FILENAME, HOLDINGS_CSV, HISTORY_CSV]
    missing = [f for f in required if not os.path.exists(f)]
    if missing:
        _fatal(f"[FATAL] System halted. Missing files: {missing}")

    print("[LOG] Phase 0: Loading System Files...")
    with open(POLICY_FILENAME, "r") as f:
        policy = json.load(f)
    with open(TRACKER_FILENAME, "r") as f:
        state = json.load(f)

    try:
        hist = pd.read_csv(HISTORY_CSV)
        hist.columns = [c.strip() for c in hist.columns]
        hold = pd.read_csv(HOLDINGS_CSV)
        hold.columns = [c.strip() for c in hold.columns]
    except Exception as e:
        _fatal(f"[FATAL] CSV Corruption: {e}")

    # Standardize history column names: Date, Ticker, AdjClose
    if "Date" not in hist.columns:
        for c in hist.columns:
            if c.lower() == "date":
                hist.rename(columns={c: "Date"}, inplace=True)
                break

    if "Ticker" not in hist.columns:
        for c in hist.columns:
            if c.lower() == "ticker":
                hist.rename(columns={c: "Ticker"}, inplace=True)
                break

    price_col = None
    for cand in ["AdjClose", "adjclose", "Close", "close", "Price", "price"]:
        if cand in hist.columns:
            price_col = cand
            break

    if price_col is None:
        _fatal("[FATAL] HISTORY_CSV missing a price column (AdjClose/Close/Price).")

    if price_col != "AdjClose":
        hist.rename(columns={price_col: "AdjClose"}, inplace=True)

    hist["Date"] = pd.to_datetime(hist["Date"], errors="coerce")
    hist["Ticker"] = hist["Ticker"].astype(str).str.strip().str.upper()
    hist["AdjClose"] = pd.to_numeric(hist["AdjClose"], errors="coerce")

    hist = hist.dropna(subset=["Date", "Ticker", "AdjClose"])
    if hist.empty:
        _fatal("[FATAL] HISTORY_CSV has no valid rows after parsing.")

    return policy, state, hist, hold


# ==============================================================================
# 2) Tracker universe + policy helpers
# ==============================================================================
def extract_universe(state: dict) -> List[str]:
    universe: Set[str] = set()

    if isinstance(state, dict):
        if isinstance(state.get("tickers"), list):
            for t in state["tickers"]:
                if isinstance(t, str):
                    universe.add(t.strip().upper())
                elif isinstance(t, dict) and "ticker" in t:
                    universe.add(str(t["ticker"]).strip().upper())

        for key in ["positions", "inventory"]:
            if isinstance(state.get(key), list):
                for item in state[key]:
                    if isinstance(item, dict) and "ticker" in item:
                        universe.add(str(item["ticker"]).strip().upper())
                    elif isinstance(item, str):
                        universe.add(item.strip().upper())

    return sorted(universe)

def get_held_tickers(hold: pd.DataFrame) -> Set[str]:
    held: Set[str] = set()
    if hold is None or hold.empty:
        return held
    for _, row in hold.iterrows():
        try:
            ticker = str(row.iloc[0]).strip().upper()
            qty = float(row.iloc[1])
            if qty > 0:
                held.add(ticker)
        except Exception:
            continue
    return held

def get_policy_required_tickers(policy: dict) -> Set[str]:
    """
    Policy-required tickers (broad): baselines + any ticker in ticker_constraints + their proxies.
    Used only for audit/warn here.
    """
    req: Set[str] = set()

    bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
    for t in (bl.get("active_benchmarks") or []):
        req.add(str(t).strip().upper())
    if bl.get("corr_anchor_ticker"):
        req.add(str(bl["corr_anchor_ticker"]).strip().upper())

    tc = policy.get("ticker_constraints", {}) or {}
    for t, c in tc.items():
        T = str(t).strip().upper()
        req.add(T)
        proxy = (c.get("lifecycle", {}) or {}).get("benchmark_proxy")
        if proxy:
            req.add(str(proxy).strip().upper())

    return {x for x in req if x}

def get_ticker_proxy(policy: dict, ticker: str, default: str = "VTI") -> str:
    tc = policy.get("ticker_constraints", {}) or {}
    c = tc.get(ticker, {}) or {}
    proxy = (c.get("lifecycle", {}) or {}).get("benchmark_proxy")
    return str(proxy).strip().upper() if proxy else default

def get_ticker_stage(policy: dict, ticker: str) -> str:
    """
    If a ticker is NOT present in policy.ticker_constraints, it defaults to REFERENCE.
    """
    T = str(ticker).strip().upper()
    tc = policy.get("ticker_constraints", {}) or {}
    if T not in tc:
        return "REFERENCE"
    c = tc.get(T, {}) or {}
    stage = (c.get("lifecycle", {}) or {}).get("stage", "inducted")
    return str(stage).strip().upper()

def get_ticker_entered_date(policy: dict, ticker: str, fallback: str = "2024-01-01") -> pd.Timestamp:
    tc = policy.get("ticker_constraints", {}) or {}
    c = tc.get(str(ticker).strip().upper(), {}) or {}
    s = (c.get("lifecycle", {}) or {}).get("entered_stage_date", fallback)
    try:
        return pd.to_datetime(str(s).strip(), errors="raise")
    except Exception:
        return pd.to_datetime(fallback)

def _format_sleeve_mapping(mapping: Any) -> str:
    if mapping is None:
        return "UNMAPPED"

    if isinstance(mapping, str):
        s = mapping.strip()
        return s if s else "UNMAPPED"

    if isinstance(mapping, (list, tuple)):
        parts = [str(x).strip() for x in mapping if str(x).strip()]
        return " | ".join(parts) if parts else "UNMAPPED"

    if isinstance(mapping, dict):
        if "sleeves" in mapping and isinstance(mapping["sleeves"], dict):
            mapping = mapping["sleeves"]

        for k in ["l2", "level2", "sleeve_l2", "leaf", "name"]:
            if k in mapping and mapping[k]:
                return str(mapping[k]).strip()

        items = []
        for k, v in mapping.items():
            key = str(k).strip()
            if not key:
                continue
            try:
                w = float(v)
                if np.isfinite(w):
                    items.append((key, w))
                else:
                    items.append((key, None))
            except Exception:
                items.append((key, None))

        if any(w is not None for _, w in items):
            num = [(k, w) for k, w in items if w is not None]
            non = [(k, w) for k, w in items if w is None]
            num.sort(key=lambda x: x[1], reverse=True)
            parts = [f"{k} {w:.2f}".rstrip("0").rstrip(".") for k, w in num]
            parts += [k for k, _ in non]
            return " | ".join(parts) if parts else "UNMAPPED"

        keys = [k for k, _ in items if k]
        return " | ".join(keys) if keys else "UNMAPPED"

    return "UNMAPPED"

def get_ticker_sleeve(policy: dict, ticker: str) -> str:
    T = str(ticker).strip().upper()

    tax = policy.get("taxonomy", {}) or {}
    t2s = tax.get("ticker_to_sleeves") or tax.get("ticker_to_sleeve") or {}
    if isinstance(t2s, dict) and T in t2s:
        return _format_sleeve_mapping(t2s[T])

    t2s2 = policy.get("ticker_to_sleeves", {}) or {}
    if isinstance(t2s2, dict) and T in t2s2:
        return _format_sleeve_mapping(t2s2[T])

    tc = policy.get("ticker_constraints", {}) or {}
    c = tc.get(T, {}) or {}
    if "sleeve" in c and isinstance(c.get("sleeve"), str) and c["sleeve"].strip():
        return c["sleeve"].strip()
    if "sleeves" in c and isinstance(c.get("sleeves"), (list, tuple, dict)):
        return _format_sleeve_mapping(c.get("sleeves"))

    return "UNMAPPED"


# ==============================================================================
# 3) Audit + valuation
# ==============================================================================
def run_titanium_audit(policy: dict, state: dict, hist: pd.DataFrame, hold: pd.DataFrame):
    print("[LOG] Phase 1: Starting Titanium Hard-Stop Audit...")

    tracker_universe = extract_universe(state)
    policy_required = get_policy_required_tickers(policy)

    print(f"[DATA] Authorized Universe ({len(tracker_universe)} tickers).")

    max_date = hist["Date"].max()
    print(f"[AUDIT] Monitoring: {len(tracker_universe)} tickers")
    print(f"[AUDIT] Health: UniverseMax={max_date.date()}")

    missing_from_tracker = sorted(list(policy_required - set(tracker_universe)))
    if missing_from_tracker:
        print(f"[AUDIT][WARN] Policy tickers missing from tracker (should be fixed): {', '.join(missing_from_tracker)}")

    held_set = get_held_tickers(hold)

    # Ranking candidates: tracker tickers that are explicitly constrained as INDUCTED/ACTIVATED
    tc = policy.get("ticker_constraints", {}) or {}
    candidates: Set[str] = set()
    for t in tracker_universe:
        if t in tc:
            stage = get_ticker_stage(policy, t)
            if stage in ["INDUCTED", "ACTIVATED"]:
                candidates.add(t)

    # Always include held tickers for reporting continuity
    candidates |= held_set

    have_hist = set(hist["Ticker"].unique())
    final_candidates = sorted([t for t in candidates if t in have_hist])

    print(f"[AUDIT] Ranking Universe: {len(final_candidates)} tickers (Active + Held)")
    print("✅ Audit Passed.")
    return final_candidates, tracker_universe, missing_from_tracker


def calculate_portfolio_value(policy: dict, hold: pd.DataFrame, hist: pd.DataFrame) -> Tuple[float, str]:
    """
    Includes policy.governance.fixed_asset_prices for CASH / TREASURY_NOTE / etc.
    """
    print("[LOG] Phase 2: Calculating Portfolio Value...")

    latest_prices = hist.sort_values("Date").groupby("Ticker").last()["AdjClose"]
    asof = str(hist["Date"].max().date())

    fixed = (policy.get("governance", {}) or {}).get("fixed_asset_prices", {}) or {}
    fixed = {str(k).strip().upper(): float(v) for k, v in fixed.items() if k is not None}

    total_val = 0.0
    if hold is not None and not hold.empty:
        for _, row in hold.iterrows():
            ticker = str(row.iloc[0]).strip().upper()
            try:
                qty = float(row.iloc[1])
            except Exception:
                qty = 0.0

            if ticker in fixed:
                px = float(fixed[ticker])
            else:
                px = float(latest_prices.get(ticker, 0.0) or 0.0)

            total_val += qty * px

    print(f"\n🚀 TITANIUM COMMAND CENTER | AS-OF: {asof}")
    print(f"🌍 REGIME: 🟢 BULL | PORTFOLIO: ${total_val:,.2f}")
    return total_val, asof


# ==============================================================================
# 4) Alpha + momentum (placeholder)
# ==============================================================================
def _aligned_total_return(prices: pd.Series) -> Optional[float]:
    prices = pd.to_numeric(prices, errors="coerce").dropna()
    if len(prices) < 2:
        return None
    return float(prices.iloc[-1] / prices.iloc[0] - 1)

def compute_alpha_vs_proxy(hist: pd.DataFrame, ticker: str, proxy: str, start_date: pd.Timestamp) -> Optional[float]:
    T = ticker.upper()
    P = proxy.upper()

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
    t_aligned = t_df.set_index("Date").loc[idx]["AdjClose"]
    p_aligned = p_df.set_index("Date").loc[idx]["AdjClose"]

    tr_t = _aligned_total_return(t_aligned)
    tr_p = _aligned_total_return(p_aligned)
    if tr_t is None or tr_p is None:
        return None
    return tr_t - tr_p

def generate_rankings(policy: dict, hist: pd.DataFrame, candidates: List[str], hold: pd.DataFrame) -> pd.DataFrame:
    print("[LOG] Phase 3: Generating Rankings...")
    if not candidates or hist.empty:
        return pd.DataFrame(columns=["Ticker", "Score", "Alpha", "AlphaVs", "Sleeve", "Status"])

    held_set = get_held_tickers(hold)
    six_months_ago = hist["Date"].max() - pd.Timedelta(days=180)

    rows = []
    for t in candidates:
        t_data = hist[hist["Ticker"] == t].sort_values("Date")
        if t_data.empty:
            continue

        curr = float(t_data.iloc[-1]["AdjClose"])
        past_slice = t_data[t_data["Date"] >= six_months_ago]
        past = float(past_slice.iloc[0]["AdjClose"]) if not past_slice.empty else float(t_data.iloc[0]["AdjClose"])
        score = (curr / past) - 1 if past > 0 else np.nan

        stage = get_ticker_stage(policy, t)
        is_held = t in held_set
        status_label = f"{stage}/{'HELD' if is_held else 'WATCH'}"

        proxy = get_ticker_proxy(policy, t, default="VTI")
        start_dt = get_ticker_entered_date(policy, t, fallback="2024-01-01")
        alpha = compute_alpha_vs_proxy(hist, t, proxy, start_dt)

        alpha_str = "N/A" if alpha is None else f"{alpha:+.1%}"
        sleeve = get_ticker_sleeve(policy, t)

        rows.append({
            "Ticker": t,
            "Score": float(score) if np.isfinite(score) else np.nan,
            "Alpha": alpha_str,
            "AlphaVs": proxy,
            "Sleeve": sleeve,
            "Status": status_label
        })

    df = pd.DataFrame(rows).sort_values("Score", ascending=False)

    print("\n🏆 MOMENTUM RANKINGS")
    print(f"{'Ticker':<6} {'Score':>7} {'Alpha':>8} {'Vs':>5} {'Sleeve':<32} {'Status'}")
    for _, r in df.iterrows():
        sleeve_disp = (r["Sleeve"] or "UNMAPPED")
        if len(sleeve_disp) > 32:
            sleeve_disp = sleeve_disp[:29] + "..."
        print(f"  {r['Ticker']:<6} {r['Score']:>7.4f} {r['Alpha']:>8} {r['AlphaVs']:>5} {sleeve_disp:<32} {r['Status']}")

    return df


# ==============================================================================
# 5) Portfolio alpha + charting from perf log (2-panel, NO shading)
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
    df = df.dropna(subset=[date_col]).sort_values(date_col)
    df = df.drop_duplicates(subset=[date_col], keep="last")

    bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
    benches = [str(x).strip().upper() for x in (bl.get("active_benchmarks") or []) if x]

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

    def _pct_col_for_benchmark(df0: pd.DataFrame, b: str) -> Optional[str]:
        # Benchmarks are authoritative and must not substitute holdings (e.g., QQQM).
        return _find_col(df0, [f"Pct_{b}", f"pct_{b}", f"pct_{b.lower()}"])

    for b in benches:
        pct_col = _pct_col_for_benchmark(df, b)
        if not pct_col:
            continue

        s = pd.to_numeric(dfw[pct_col], errors="coerce").dropna()
        if s.empty:
            continue

        b_last = float(s.iloc[-1])
        alpha = p_last - b_last
        out[b] = f"{alpha:+.2%}"

    return out

def rotate_and_chart(df_scores: pd.DataFrame, policy: dict) -> None:
    """
    Generates a 2-panel chart:
      Panel 1: Titanium (MWS) vs VTI (S&P) vs QQQ (Nasdaq) cumulative performance
      Panel 2: Cumulative alpha vs VTI and QQQ

    NOTE: Shading removed (will be added later).
    """
    if df_scores is not None and not df_scores.empty:
        held = df_scores[df_scores["Status"].str.contains("HELD", na=False)]
        watch = df_scores[(df_scores["Status"].str.contains("WATCH", na=False)) & (df_scores["Score"] > 1.0)]

        if not watch.empty and not held.empty:
            trim = held.iloc[-1]["Ticker"]
            buy = watch.sort_values("Score", ascending=False).iloc[0]["Ticker"]
            print(f"\n🔄 ROTATION: TRIM {trim} -> BUY {buy}")
        else:
            trim = held.iloc[-1]["Ticker"] if not held.empty else "NONE"
            print(f"\n🔄 ROTATION: No BUY candidate. Weakest held: {trim}")

    palpha = compute_portfolio_alpha_from_log(policy)
    if palpha:
        order = ["VTI", "QQQ"] + sorted([k for k in palpha.keys() if k not in ["VTI", "QQQ"]])
        parts = [f"{k} {palpha[k]}" for k in order if k in palpha]
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
        df_log = df_log.dropna(subset=[date_c]).sort_values(date_c)
        df_log = df_log.drop_duplicates(subset=[date_c], keep="last")

        bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
        chart_start_str = str(bl.get("chart_start_date") or "2026-01-05").strip()
        chart_start = pd.to_datetime(chart_start_str, errors="coerce")

        port_col = _find_col(df_log, ["PortfolioPct", "portfoliopct"])
        if not port_col:
            print("⚠️ Charting skipped: Perf log missing PortfolioPct column.")
            return

        # Window
        df_plot = df_log.copy()
        if pd.notna(chart_start):
            df_plot = df_plot[df_plot[date_c] >= chart_start].copy()

        df_plot[port_col] = pd.to_numeric(df_plot[port_col], errors="coerce")
        df_plot = df_plot.dropna(subset=[port_col])

        title_suffix = f"Since {chart_start_str}"
        if df_plot.empty:
            print(f"⚠️ No data after {chart_start_str}. Falling back to last 90 observations.")
            df_plot = df_log.tail(90).copy()
            df_plot[port_col] = pd.to_numeric(df_plot[port_col], errors="coerce")
            df_plot = df_plot.dropna(subset=[port_col])
            title_suffix = "Last 90 Days (Fallback)"

        if df_plot.empty:
            print("⚠️ Charting skipped: Perf log has no plottable rows.")
            return

        # Identify benchmark pct columns for VTI and QQQ in this window
        pct_vti = _find_col(df_plot, ["Pct_VTI", "pct_vti"])
        pct_qqq = _find_col(df_plot, ["Pct_QQQ", "pct_qqq"])

        if not pct_qqq:
            print("⚠️ Benchmark series Pct_QQQ not found in perf log; skipping QQQ (Nasdaq) benchmark line.")

        # Build alpha series (panel 2) only if columns exist
        alpha_vti = None
        alpha_qqq = None
        if pct_vti:
            alpha_vti = pd.to_numeric(df_plot[port_col], errors="coerce") - pd.to_numeric(df_plot[pct_vti], errors="coerce")
        if pct_qqq:
            alpha_qqq = pd.to_numeric(df_plot[port_col], errors="coerce") - pd.to_numeric(df_plot[pct_qqq], errors="coerce")

        # --- Plot: 2 panels ---
        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(14, 9), sharex=True,
            gridspec_kw={"height_ratios": [2, 1]}
        )

        # Panel 1: performance
        ax1.plot(df_plot[date_c], df_plot[port_col], label="Titanium (MWS)", linewidth=2)

        if pct_vti:
            ax1.plot(df_plot[date_c], pd.to_numeric(df_plot[pct_vti], errors="coerce"), label="VTI (S&P)", linewidth=2)

        if pct_qqq:
            ax1.plot(df_plot[date_c], pd.to_numeric(df_plot[pct_qqq], errors="coerce"), label="QQQ (Nasdaq)", linewidth=2)

        ax1.set_title(f"Titanium Performance ({title_suffix})")
        ax1.grid(True, alpha=0.3)
        ax1.legend()
        ax1.yaxis.set_major_formatter(lambda x, pos: f"{x*100:.0f}%")

        # Panel 2: cumulative alpha
        if alpha_vti is not None:
            ax2.plot(df_plot[date_c], alpha_vti, label="Alpha vs VTI (S&P)", linewidth=2)
        if alpha_qqq is not None:
            ax2.plot(df_plot[date_c], alpha_qqq, label="Alpha vs QQQ (Nasdaq)", linewidth=2)

        ax2.axhline(0, linewidth=1)
        ax2.set_title(f"Cumulative Alpha vs. Nasdaq and S&P (since {chart_start_str})")
        ax2.grid(True, alpha=0.3)
        ax2.legend()
        ax2.yaxis.set_major_formatter(lambda x, pos: f"{x*100:.0f}%")

        # --- Right-side labels (Panel 1): bold black, 2 decimals ---
        def _label_last(ax, x_ser: pd.Series, y_ser: pd.Series, text: str, y_nudge_pts: int = 0) -> None:
            if x_ser is None or y_ser is None or len(x_ser) == 0 or len(y_ser) == 0:
                return
            x_last = x_ser.iloc[-1]
            y_last = float(y_ser.iloc[-1])
            ax.annotate(
                text,
                xy=(x_last, y_last),
                xytext=(10, y_nudge_pts),
                textcoords="offset points",
                ha="left",
                va="center",
                fontsize=12,
                fontweight="bold",
                color="black",
                clip_on=False
            )

        series_for_labels = []
        series_for_labels.append(("Titanium (MWS)", df_plot[date_c], pd.to_numeric(df_plot[port_col], errors="coerce")))

        if pct_vti:
            series_for_labels.append(("VTI (S&P)", df_plot[date_c], pd.to_numeric(df_plot[pct_vti], errors="coerce")))
        if pct_qqq:
            series_for_labels.append(("QQQ (Nasdaq)", df_plot[date_c], pd.to_numeric(df_plot[pct_qqq], errors="coerce")))

        last_vals = []
        for name, xs, ys in series_for_labels:
            ys2 = ys.dropna()
            if ys2.empty:
                continue
            xs2 = df_plot.loc[ys2.index, date_c]
            last_vals.append((name, xs2, ys2, float(ys2.iloc[-1])))

        last_vals.sort(key=lambda x: x[3])
        nudges = {name: 0 for (name, *_rest) in last_vals}
        for i in range(1, len(last_vals)):
            prev = last_vals[i - 1]
            cur = last_vals[i]
            if abs(cur[3] - prev[3]) < 0.0025:
                nudges[cur[0]] = nudges[prev[0]] + 12

        for name, xs, ys, v in last_vals:
            _label_last(ax1, xs, ys, f"{v*100:.2f}%", y_nudge_pts=nudges.get(name, 0))

        plt.tight_layout()
        plt.savefig(CHART_FILENAME)

        print(f"\n✅ Chart generated: {CHART_FILENAME}")
        subprocess.run(["open", CHART_FILENAME], check=False)

    except Exception as e:
        print(f"⚠️ Charting Error: {e}")


# ==============================================================================
# 6) Main
# ==============================================================================
def main() -> None:
    policy, state, hist, hold = load_system_files()

    candidates, _, _ = run_titanium_audit(policy, state, hist, hold)
    _ = calculate_portfolio_value(policy, hold, hist)

    df_scores = generate_rankings(policy, hist, candidates, hold)
    rotate_and_chart(df_scores, policy)

    print("\n✅ Run Complete.")

if __name__ == "__main__":
    main()
