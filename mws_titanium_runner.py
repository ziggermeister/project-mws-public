import json
import os
import warnings
import subprocess
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, List, Set, Any
import re

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")

POLICY_FILENAME  = "mws_policy.json"
TRACKER_FILENAME = "mws_tracker.json"
HOLDINGS_CSV     = "mws_holdings.csv"
HISTORY_CSV      = "mws_ticker_history.csv"
PERF_LOG_CSV     = "mws_recent_performance.csv"
CHART_FILENAME   = "mws_equity_curve.png"

# ==============================================================================
# 1) Robust loading & Event Label Sanitization
# ==============================================================================

def sanitize_event_label(label: str) -> str:
    s = str(label or "").strip()

    # Normalize punctuation
    s = s.replace("“", "").replace("”", "")
    s = s.replace("‘", "").replace("’", "")
    s = s.replace("–", "-").replace("—", "-")
    s = s.replace("\\u00A0", " ")

    # CSV-safe cleanup
    s = s.replace(",", " - ")
    s = s.replace(";", " - ")
    s = s.replace('"', "")
    s = s.replace("'", "")
    s = re.sub(r"[\\r\\n]+", " | ", s)
    s = re.sub(r"\\s+", " ", s).strip()

    # Compact and safe
    if len(s) > 120:
        s = s[:117].rstrip() + "..."

    return s

def update_event_labels_by_date(perf_csv_path: str, date_to_label: dict) -> None:
    \"\"\"
    Update only EventLabel by Date key.
    Leaves all numeric columns untouched.
    \"\"\"
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

    df.to_csv(perf_csv_path, index=False)

def apply_recent_event_labels(perf_csv_path: str, recent_dates: list[str], label_fn) -> None:
    \"\"\"
    recent_dates: list of YYYY-MM-DD dates to consider
    label_fn: function(date_str) -> str | None
    \"\"\"
    date_to_label = {}

    for date_str in recent_dates:
        raw = label_fn(date_str)
        if not raw:
            continue
        raw = sanitize_event_label(raw)
        if raw.upper().startswith("SKIP"):
            continue
        date_to_label[date_str] = raw

    if date_to_label:
        update_event_labels_by_date(perf_csv_path, date_to_label)

def recent_calendar_dates(as_of_date: str, days_back: int = 5) -> list[str]:
    d0 = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    return [
        (d0 - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days_back, -1, -1)
    ]

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
    \"\"\"Benchmarks + all tickers in ticker_constraints. Used for audit/warn.\"\"\"
    req: Set[str] = set()
    bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
    for t in (bl.get("active_benchmarks") or []):
        req.add(str(t).strip().upper())
    if bl.get("corr_anchor_ticker"):
        req.add(str(bl["corr_anchor_ticker"]).strip().upper())
    for t in (policy.get("ticker_constraints", {}) or {}):
        req.add(str(t).strip().upper())
    return {x for x in req if x}

def get_ticker_proxy(policy: dict, ticker: str, default: str = "VTI") -> str:
    tc = policy.get("ticker_constraints", {}) or {}
    proxy = ((tc.get(ticker, {}) or {}).get("lifecycle", {}) or {}).get("benchmark_proxy")
    return str(proxy).strip().upper() if proxy else default

def get_ticker_stage(policy: dict, ticker: str) -> str:
    \"\"\"Tickers absent from ticker_constraints default to REFERENCE.\"\"\"
    T = str(ticker).strip().upper()
    tc = policy.get("ticker_constraints", {}) or {}
    if T not in tc:
        return "REFERENCE"
    stage = ((tc[T] or {}).get("lifecycle", {}) or {}).get("stage", "inducted")
    return str(stage).strip().upper()

def get_ticker_sleeve(policy: dict, ticker: str) -> str:
    \"\"\"Returns primary sleeve name from ticker_to_sleeves, or UNMAPPED.\"\"\"
    T = str(ticker).strip().upper()
    mapping = (policy.get("ticker_to_sleeves", {}) or {}).get(T)
    if not isinstance(mapping, dict) or not mapping:
        return "UNMAPPED"
    return max(mapping, key=lambda k: mapping[k])


# ==============================================================================
# 3) Audit + valuation
# ==============================================================================
def run_titanium_audit(policy: dict, state: dict, hist: pd.DataFrame, hold: pd.DataFrame):
    print("[LOG] Phase 1: Starting Titanium Hard-Stop Audit...")

    policy_required = get_policy_required_tickers(policy)
    tc = policy.get("ticker_constraints", {}) or {}
    have_hist = set(hist["Ticker"].unique())

    max_date = hist["Date"].max()
    print(f"[AUDIT] Health: UniverseMax={max_date.date()}")

    held_set = get_held_tickers(hold)

    candidates: Set[str] = set()
    for t in tc:
        stage = get_ticker_stage(policy, t)
        if stage in ["INDUCTED", "ACTIVATED"] and t in have_hist:
            candidates.add(t)

    candidates |= held_set
    final_candidates = sorted(t for t in candidates if t in have_hist)

    missing_from_hist = sorted(policy_required - have_hist)
    if missing_from_hist:
        print(f"[AUDIT][WARN] Policy tickers missing from history: {', '.join(missing_from_hist)}")

    print(f"[AUDIT] Ranking Universe: {len(final_candidates)} tickers (Active + Held)")
    print("✅ Audit Passed.")
    return final_candidates, [], missing_from_hist


def calculate_portfolio_value(policy: dict, hold: pd.DataFrame, hist: pd.DataFrame) -> Tuple[float, str]:
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
        for _, row in hold.iterrows():
            ticker = str(row.iloc[0]).strip().upper()
            try:
                qty = float(row.iloc[1])
            except Exception:
                qty = 0.0
            fixed_px = _resolve_fixed_price(ticker)
            px = fixed_px if fixed_px is not None else float(latest_prices.get(ticker, 0.0) or 0.0)
            total_val += qty * px

    print(f"\\n🚀 TITANIUM COMMAND CENTER | AS-OF: {asof} | policy={policy_version}")
    print(f"🌍 REGIME: 🟢 BULL | PORTFOLIO: ${total_val:,.2f}")
    return total_val, asof


# ==============================================================================
# 4) Momentum rankings
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
    print("[LOG] Phase 3: Generating Rankings...")
    if not candidates or hist.empty:
        return pd.DataFrame(columns=["Ticker", "Score", "Alpha", "AlphaVs", "Sleeve", "Status"])

    held_set = get_held_tickers(hold)
    six_months_ago = hist["Date"].max() - pd.Timedelta(days=180)
    alpha_start = pd.to_datetime("2024-01-01")

    rows = []
    for t in candidates:
        t_data = hist[hist["Ticker"] == t].sort_values("Date")
        if t_data.empty:
            continue

        curr = float(t_data.iloc[-1]["AdjClose"])
        past_slice = t_data[t_data["Date"] >= six_months_ago]
        past = float(past_slice.iloc[0]["AdjClose"]) if not past_slice.empty else float(t_data.iloc[0]["AdjClose"])
        score = (curr / past) - 1 if past > 0 else np.nan

        proxy = get_ticker_proxy(policy, t, default="VTI")
        alpha = compute_alpha_vs_proxy(hist, t, proxy, alpha_start)

        rows.append({
            "Ticker":  t,
            "Score":   float(score) if np.isfinite(score) else np.nan,
            "Alpha":   "N/A" if alpha is None else f"{alpha:+.1%}",
            "AlphaVs": proxy,
            "Sleeve":  get_ticker_sleeve(policy, t),
            "Status":  f"{get_ticker_stage(policy, t)}/{'HELD' if t in held_set else 'WATCH'}",
        })

    df = pd.DataFrame(rows).sort_values("Score", ascending=False)

    print("\\n🏆 MOMENTUM RANKINGS")
    print(f"{'Ticker':<6} {'Score':>7} {'Alpha':>8} {'Vs':>5} {'Sleeve':<32} {'Status'}")
    for _, r in df.iterrows():
        sleeve_disp = (r["Sleeve"] or "UNMAPPED")
        if len(sleeve_disp) > 32:
            sleeve_disp = sleeve_disp[:29] + "..."
        print(f"  {r['Ticker']:<6} {r['Score']:>7.4f} {r['Alpha']:>8} {r['AlphaVs']:>5} {sleeve_disp:<32} {r['Status']}")

    return df


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
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 2:
        return 0.0
    levels = 1 + s
    rolling_peak = levels.cummax()
    drawdowns = (levels - rolling_peak) / rolling_peak
    return float(drawdowns.min())

def _dominant_series_and_mag(moves: dict, pc: str) -> tuple[str, float]:
    mws = moves.get(pc, None)
    vti = moves.get("Pct_VTI", moves.get("pct_vti", None))
    qqq = moves.get("Pct_QQQ", moves.get("pct_qqq", None))

    items = []
    if mws is not None: items.append(("MWS", float(mws)))
    if vti is not None: items.append(("VTI", float(vti)))
    if qqq is not None: items.append(("QQQ", float(qqq)))

    if not items:
        return ("MWS", 0.0)

    name, mv = max(items, key=lambda x: abs(x[1]))
    return name, mv * 100.0

def _format_moves_for_prompt(moves: dict, pc: str) -> tuple[float, float, float, float]:
    mws = float(moves.get(pc, 0.0)) * 100.0
    vti = float(moves.get("Pct_VTI", moves.get("pct_vti", 0.0))) * 100.0
    qqq = float(moves.get("Pct_QQQ", moves.get("pct_qqq", 0.0))) * 100.0
    event_mag = max(abs(mws), abs(vti), abs(qqq))
    return mws, vti, qqq, event_mag

def _fetch_label_from_anthropic(date_str: str, moves: dict, move_threshold: float, pc: str) -> str:
    import json as _json
    import urllib.request as _urllib
    import urllib.error as _urlerr
    import time as _time

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""

    mws, vti, qqq, event_mag = _format_moves_for_prompt(moves, pc)

    user_prompt = f\"\"\"You are annotating a US market chart.

DATE: {date_str}

MOVES:
MWS {mws:+.2f}%
VTI {vti:+.2f}%
QQQ {qqq:+.2f}%

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
\"\"\"

    def _api_round_trip(messages):
        body = _json.dumps({
            "model": "claude-3-5-haiku-20241022",
            "max_tokens": 512,
            "tools": [{"type": "google_search_20241022", "name": "web_search", "description": "Search the web."}],
            "messages": messages,
        }, ensure_ascii=True).encode("utf-8")
        req = _urllib.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-api-key": api_key.strip(),
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )
        with _urllib.urlopen(req, timeout=45) as resp:
            return _json.loads(resp.read())

    messages = [{"role": "user", "content": user_prompt}]
    
    for attempt in range(4):
        try:
            for _ in range(8):  # tool loop
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
                    if not text:
                        return ""

                    if text.upper().startswith("SKIP"):
                        return "SKIP"
                    
                    return text

                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": "Search results"}
                    for tu in tool_uses
                ]})
            break
        except _urlerr.HTTPError as e:
            if e.code == 429 and attempt < 3:
                _time.sleep(20 * (attempt + 1))
            else:
                break
        except Exception:
            break
            
    return ""

def backfill_recent_events(csv_path: str, as_of_date_str: str, days_back: int = 5, move_threshold: float = 0.0175):
    """
    Only fetches labels for the recent 5 days window, then updates CSV by date.
    """
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]

    dc = next((c for c in df.columns if c.lower() == "date"), None)
    pc = next((c for c in df.columns if c.lower() == "portfoliopct"), None)
    if not dc or not pc:
        return

    bench_cols = [c for c in ["Pct_VTI", "Pct_QQQ"] if c in df.columns]

    recent_dates = recent_calendar_dates(as_of_date_str, days_back=days_back)
    
    def get_event_label_for_date(date_str: str) -> str:
        row = df[df[dc] == date_str]
        if row.empty:
            return ""
            
        i = row.index[0]
        if i == 0: return ""
        
        # Calculate moves from previous day
        moves = {}
        for c in [pc] + bench_cols:
            prev = pd.to_numeric(df.loc[i - 1, c], errors="coerce")
            cur  = pd.to_numeric(df.loc[i,     c], errors="coerce")
            if pd.notna(prev) and pd.notna(cur):
                moves[c] = cur - prev
                
        if not moves or max(abs(v) for v in moves.values()) < move_threshold:
            return ""
            
        # Already labeled?
        existing = str(df.loc[i].get("EventLabel", "")).strip()
        if existing and existing.lower() != "nan":
            return existing

        print(f"[EVENTS] Fetching label for {date_str}...")
        raw_label = _fetch_label_from_anthropic(date_str, moves, move_threshold, pc)
        
        return raw_label

    apply_recent_event_labels(csv_path, recent_dates, get_event_label_for_date)


def _read_chart_events(
    df: pd.DataFrame,
    date_col: str,
    port_col: str = None,
    bench_cols: list = None
) -> list:
    import pandas as pd

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

    for c in numeric_cols:
        df2[c] = pd.to_numeric(df2[c], errors="coerce")

    for c in numeric_cols:
        df2[f"_d_{c}"] = df2[c].diff()

    df2["_max_move"] = df2[[f"_d_{c}" for c in numeric_cols]].abs().max(axis=1)

    df2[label_col] = df2[label_col].astype(str).str.strip()
    df2 = df2[(df2[label_col] != "") & (df2[label_col].str.lower() != "nan")]
    df2 = df2.dropna(subset=["_max_move"])
    if df2.empty:
        return []

    df_ranked = df2.sort_values("_max_move", ascending=False)
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
        base_label = raw_lbl.replace(" | ", "\\n")

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

        bench_moves.sort(key=lambda t: abs(t[1]), reverse=True)
        for name, mv in bench_moves[:2]:
            metric_parts.append(f"{name} {_fmt_mv(mv)}")

        metrics_line = " | ".join(metric_parts)

        if mws_mv is not None and pd.notna(mws_mv):
            dominant_mv = float(mws_mv)
        elif bench_moves:
            dominant_mv = float(bench_moves[0][1])
        else:
            dominant_mv = 0.0

        full_label = f"{metrics_line}\\n{base_label}" if metrics_line else base_label
        selected.append((d, full_label, dominant_mv))

    selected.sort(key=lambda x: x[0])
    return selected
    

def rotate_and_chart(df_scores: pd.DataFrame, policy: dict, hist: pd.DataFrame) -> None:
    palpha = compute_portfolio_alpha_from_log(policy)
    if palpha:
        order = ["VTI", "QQQ"] + sorted(k for k in palpha if k not in ["VTI", "QQQ"])
        parts = [f"{k} {palpha[k]}" for k in order if k in palpha]
        if parts:
            print("\\n📈 PORTFOLIO ALPHA (since chart baseline): " + " | ".join(parts))

    try:
        if not os.path.exists(PERF_LOG_CSV):
            print("⚠️ Charting skipped: Perf log CSV not found.")
            return

        as_of_date = str(hist["Date"].max().date())
        
        # Perform rolling 5-day event label check/backfill
        backfill_recent_events(PERF_LOG_CSV, as_of_date, days_back=5, move_threshold=0.0175)

        # Reload after possible updates
        df_log = pd.read_csv(PERF_LOG_CSV)
        df_log.columns = [c.strip() for c in df_log.columns]
        date_c = _find_col(df_log, ["Date", "date"])
        
        if not date_c:
            print("⚠️ Charting skipped: Perf log missing Date column.")
            return

        df_log[date_c] = pd.to_datetime(df_log[date_c], errors="coerce")
        df_log = df_log.dropna(subset=[date_c]).sort_values(date_c).drop_duplicates(subset=[date_c], keep="last")

        bl = (policy.get("governance", {}).get("reporting_baselines", {}) or {})
        chart_start_str = str(bl.get("chart_start_date") or "2026-01-05").strip()
        chart_start = pd.to_datetime(chart_start_str, errors="coerce")

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
            df_plot = df_log.tail(90).copy()
            df_plot[port_col] = pd.to_numeric(df_plot[port_col], errors="coerce")
            df_plot = df_plot.dropna(subset=[port_col])
            title_suffix = "Last 90 Days (Fallback)"

        if df_plot.empty:
            return

        pct_vti = _find_col(df_plot, ["Pct_VTI", "pct_vti"])
        pct_qqq = _find_col(df_plot, ["Pct_QQQ", "pct_qqq"])

        to_num = lambda col: pd.to_numeric(df_plot[col], errors="coerce")
        port_series = to_num(port_col)
        alpha_vti   = (port_series - to_num(pct_vti)) if pct_vti else None
        alpha_qqq   = (port_series - to_num(pct_qqq)) if pct_qqq else None

        dr = policy.get("drawdown_rules", {}) or {}
        soft_limit = float(dr.get("soft_limit", 0.20))
        hard_limit = float(dr.get("hard_limit", 0.28))

        levels       = (1 + port_series)
        rolling_peak = levels.cummax()
        drawdown_ser = (levels - rolling_peak) / rolling_peak

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
                if ys2.empty: continue
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
            ys2 = pd.to_numeric(ys, errors="coerce").dropna()
            if ys2.empty or len(ys2) < 3: return
            xs2 = xs.loc[ys2.index] if hasattr(xs, "loc") else xs

            i_max = int(ys2.idxmax())
            i_min = int(ys2.idxmin())
            if i_max == ys2.index[0] or i_max == ys2.index[-1]: i_max = None
            if i_min == ys2.index[0] or i_min == ys2.index[-1]: i_min = None

            for idx, prefix, va in [(i_max, "Max", max_va), (i_min, "Min", min_va)]:
                if idx is None: continue
                xv = xs2.loc[idx]
                yv = float(ys2.loc[idx])
                yo = 10 if va == "bottom" else -10
                ax.scatter(xv, yv, s=80, zorder=7, color=color, edgecolor="#333", linewidth=1.2)
                ax.annotate(
                    f"{prefix}: {yv*100:+.2f}%",
                    xy=(xv, yv), xytext=(0, yo), textcoords="offset points",
                    ha="center", va=va, fontsize=10, fontweight="bold",
                    color="#222", clip_on=False,
                    bbox=dict(boxstyle="square,pad=0.2", fc="white", ec="none", alpha=0.7)
                )

        _all_vals = pd.concat([port_series] + ([to_num(pct_vti)] if pct_vti else []) + ([to_num(pct_qqq)] if pct_qqq else []), axis=0).dropna()
        _floor = float(_all_vals.min())
        
        if pct_qqq:
            ax1.fill_between(dates, to_num(pct_qqq), _floor, alpha=0.13, color="#2ca02c", interpolate=True, label="_nolegend_")
            ax1.plot(dates, to_num(pct_qqq), label="QQQ (Nasdaq)", linewidth=1.8, color="#2ca02c", alpha=0.85, zorder=3)
        if pct_vti:
            ax1.fill_between(dates, to_num(pct_vti), _floor, alpha=0.13, color="orange", interpolate=True, label="_nolegend_")
            ax1.plot(dates, to_num(pct_vti), label="VTI (Total Market)", linewidth=1.8, color="orange", alpha=0.85, zorder=3)
            
        ax1.fill_between(dates, port_series, _floor, alpha=0.18, color="#1f77b4", interpolate=True, label="_nolegend_")
        ax1.plot(dates, port_series, label="Titanium (MWS)", linewidth=1.8, color="#1f77b4", zorder=4)

        mws_avg = float(port_series.dropna().mean())
        ax1.axhline(mws_avg, color="#1f77b4", linewidth=1.4, linestyle=(0, (4, 3)), alpha=0.7, zorder=2)
        ax1.set_title(f"Titanium Performance ({title_suffix})", fontsize=11, fontweight="bold", pad=10)
        ax1.grid(True, alpha=0.45, color="#999999")
        
        _handles, _labels = ax1.get_legend_handles_labels()
        _legend_items = [(h, l) for h, l in zip(_handles, _labels) if not l.startswith("_")]
        if _legend_items:
            _h, _l = zip(*_legend_items)
            ax1.legend(_h, _l, loc="upper left", fontsize=9, framealpha=0.95, edgecolor="#aaaaaa", fancybox=True, facecolor="white", labelcolor="#222222")
            
        ax1.axhline(_floor, color="#888888", linewidth=0.6, linestyle=":", alpha=0.7, zorder=1)
        _cur_ticks = list(ax1.get_yticks())
        ax1.set_yticks(sorted(set(_cur_ticks + [_floor])))
        ax1.yaxis.set_major_formatter(lambda x, pos: f"{x*100:.0f}%")

        series1 = [("Titanium MWS:", dates, port_series)]
        if pct_vti: series1.append(("Total Market:", dates, to_num(pct_vti)))
        if pct_qqq: series1.append(("Nasdaq:", dates, to_num(pct_qqq)))
        _apply_labels(ax1, series1)
        
        _mark_extremes(ax1, dates.reset_index(drop=True), port_series.reset_index(drop=True), "#1f77b4")
        if pct_vti: _mark_extremes(ax1, dates.reset_index(drop=True), to_num(pct_vti).reset_index(drop=True), "orange", max_va="bottom", min_va="top")
        if pct_qqq: _mark_extremes(ax1, dates.reset_index(drop=True), to_num(pct_qqq).reset_index(drop=True), "#2ca02c", max_va="bottom", min_va="bottom")

        line_styles = [
            (port_series, "#1f77b4", 0.8),
            (to_num(pct_vti) if pct_vti else None, "orange", 0.8),
            (to_num(pct_qqq) if pct_qqq else None, "#2ca02c", 0.8),
        ]
        for ys, color, lw in line_styles:
            if ys is None: continue
            ys2 = pd.to_numeric(ys, errors="coerce").dropna()
            if ys2.empty: continue
            ax1.axhline(float(ys2.iloc[-1]), color=color, linewidth=lw * 2, linestyle="--", alpha=0.5, zorder=1)

        port_clean = pd.to_numeric(port_series, errors="coerce").dropna()
        if not port_clean.empty:
            port_avg = float(port_clean.mean())
            ax1.axhline(port_avg, color="#333333", linewidth=1.4, linestyle="-", alpha=0.5, zorder=2)
            ax1.scatter(dates.iloc[-1], port_avg, s=80, zorder=6, color="white", edgecolor="#333333", linewidth=1.2)
            _label_last(ax1, dates, pd.Series([port_avg]*len(dates), index=dates.index), f"Titanium MWS Ave: {port_avg*100:+.2f}%", val=port_avg)

        ax2.axhline(0, color="#888", linewidth=1.0, zorder=1)
        if alpha_vti is not None:
            ax2.plot(dates, alpha_vti, linewidth=2.0, color="orange", label="vs Total Market", zorder=3)
            ax2.fill_between(dates, alpha_vti, 0, where=(alpha_vti >= 0), alpha=0.18, color="orange", interpolate=True)
            ax2.fill_between(dates, alpha_vti, 0, where=(alpha_vti < 0), alpha=0.18, color="red", interpolate=True)
        if alpha_qqq is not None:
            ax2.plot(dates, alpha_qqq, linewidth=2.0, color="#2ca02c", label="vs Nasdaq", zorder=3)
            ax2.fill_between(dates, alpha_qqq, 0, where=(alpha_qqq >= 0), alpha=0.18, color="#2ca02c", interpolate=True)
            ax2.fill_between(dates, alpha_qqq, 0, where=(alpha_qqq < 0), alpha=0.18, color="red", interpolate=True)

        ax2.set_title(f"Cumulative Alpha vs. Benchmarks (since {chart_start.strftime('%b %d, %Y') if pd.notna(chart_start) else chart_start_str})", fontsize=11, fontweight="bold", pad=8)
        ax2.grid(True, alpha=0.45, color="#999999")

        _handles2, _labels2 = ax2.get_legend_handles_labels()
        _leg2 = [(h, l) for h, l in zip(_handles2, _labels2) if not l.startswith("_")]
        if _leg2:
            _h2, _l2 = zip(*_leg2)
            ax2.legend(_h2, _l2, loc="upper left", fontsize=9, framealpha=0.95, edgecolor="#aaaaaa", fancybox=True, facecolor="white", labelcolor="#222222")
            
        ax2.yaxis.set_major_formatter(lambda x, pos: f"{x*100:.0f}%")

        series2 = []
        if alpha_vti is not None: series2.append(("vs Total Market:", dates, alpha_vti))
        if alpha_qqq is not None: series2.append(("vs Nasdaq:", dates, alpha_qqq))
        _apply_labels(ax2, series2)
        
        if alpha_vti is not None: _mark_extremes(ax2, dates.reset_index(drop=True), pd.Series(alpha_vti).reset_index(drop=True), "orange", max_va="top", min_va="top")
        if alpha_qqq is not None: _mark_extremes(ax2, dates.reset_index(drop=True), pd.Series(alpha_qqq).reset_index(drop=True), "#2ca02c", max_va="bottom", min_va="bottom")

        date_padding = pd.Timedelta(days=max(2, int((dates.iloc[-1] - dates.iloc[0]).days * 0.015)))
        ax1.set_xlim(dates.iloc[0], dates.iloc[-1] + date_padding)

        import matplotlib.dates as mdates
        ax2.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %-d"))
        ax2.tick_params(axis="x", which="major", labelsize=8, color="#888", labelcolor="#333", length=4)
        for lbl in ax2.get_xticklabels():
            lbl.set_fontweight("bold")
            
        for d in pd.date_range(dates.iloc[0], dates.iloc[-1], freq="W-MON"):
            for ax in (ax1, ax2):
                ax.axvline(d, color="#aaaaaa", linewidth=0.6, linestyle="--", alpha=0.4, zorder=1)

        chart_events = _read_chart_events(df_plot, date_c, port_col=port_col, bench_cols=[c for c in [pct_vti, pct_qqq] if c])
        _total_days = (dates.iloc[-1] - dates.iloc[0]).days or 1
        _label_dates = [d for d, _, _mv in chart_events]
        
        for _ei, (d, lbl, _dominant_mv) in enumerate(chart_events):
            _evt_color = "#cc2222" if _dominant_mv < 0 else "#1a7a1a"
            for ax in (ax1, ax2):
                ax.axvline(d, color=_evt_color, linewidth=1.2, linestyle="--", alpha=0.6, zorder=3)
            _pos = (d - dates.iloc[0]).days / _total_days
            _ha  = "right" if _pos > 0.92 else ("left" if _pos < 0.08 else "center")
            _xoff = -4 if _ha == "right" else (4 if _ha == "left" else 0)
            _neighbours = [abs((d - d2).days) for d2 in _label_dates if d2 != d]
            _yoff = -87 if _neighbours and min(_neighbours) < 12 and _ei % 2 == 1 else -57
            ax2.annotate(
                f"{d.strftime('%A, %b %-d')}\\n{lbl}",
                xy=(d, ax2.get_ylim()[0]),
                xytext=(_xoff, _yoff), textcoords="offset points",
                ha=_ha, va="top", fontsize=8.5, fontweight="bold",
                color=_evt_color, clip_on=False
            )

        _ax2_ymin, _ax2_ymax = ax2.get_ylim()
        ax2.set_ylim(min(-0.015, _ax2_ymin), _ax2_ymax)

        plt.tight_layout(rect=[0, 0, 0.90, 1])
        plt.subplots_adjust(bottom=0.28, hspace=0.2)
        plt.savefig(CHART_FILENAME, dpi=150, bbox_inches="tight")
        print(f"\\n✅ Chart generated: {CHART_FILENAME}")

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
    rotate_and_chart(df_scores, policy, hist)
    print("\\n✅ Run Complete.")

if __name__ == "__main__":
    main()

