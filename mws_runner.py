#!/usr/bin/env python3
"""
mws_runner.py — MWS portfolio run orchestrator (GitHub Actions + local).

Runs automatically on weekdays at market open +30 min (14:30 UTC) and
market close +30 min (21:30 UTC) via GitHub Actions cron,
or on-demand via workflow_dispatch. Works entirely without a local laptop.

Architecture:
  1. Import mws_analytics to compute momentum scores + breach flags
  2. Build a structured context bundle (rankings, portfolio value, drawdown state)
  3. Call Claude API with web_search tool — Claude executes the full 8-step run protocol
     defined in mws_policy.json → news_intelligence.run_protocol.llm_run_sequence
  4. Extract mws_market_context.md from Claude's response and write to disk
  5. Email full recommendation via Gmail SMTP (if GMAIL_APP_PASSWORD set)

Required GitHub Secrets:  ANTHROPIC_API_KEY
Optional GitHub Secrets:  GMAIL_APP_PASSWORD, GMAIL_FROM, GMAIL_TO
"""

# Set headless matplotlib backend BEFORE any other imports
import matplotlib
matplotlib.use("Agg")

import json
import logging
import os
import re
import smtplib
import sys
import traceback
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown as md

import anthropic
import pandas as pd

# Now safe to import mws_analytics and mws_charts (matplotlib already set to Agg)
import mws_analytics
import mws_charts

# ── Constants ──────────────────────────────────────────────────────────────────
TODAY          = datetime.now().strftime("%Y-%m-%d")
TRIGGER_REASON = os.environ.get("TRIGGER_REASON", "scheduled")
# Model is env-configurable — set ANTHROPIC_MODEL secret in GitHub Actions to override.
# claude-3-5-sonnet-20241022 is the default: widely available, supports web_search_20250305.
MODEL          = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-5-20250929"
MAX_TOKENS     = 16000
# Schema validation bounds — guard against model denial-of-service via huge outputs
MAX_RESPONSE_CHARS   = 120_000   # ~30k tokens; alert if exceeded
MAX_BLOCK_CHARS      = 60_000    # each XML block independently capped for parsing safety
# File paths — single source of truth lives in mws_analytics; imported here
POLICY_FILE     = mws_analytics.POLICY_FILENAME
TRACKER_FILE    = mws_analytics.TRACKER_FILENAME
HOLDINGS_FILE   = mws_analytics.HOLDINGS_CSV
HISTORY_FILE    = mws_analytics.HISTORY_CSV
MACRO_FILE      = mws_analytics.MACRO_MD
MARKET_CTX_FILE = mws_analytics.MARKET_CTX_MD
RESULTS_FILE    = mws_analytics.RESULTS_CSV

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mws_runner")


# ── Step 1: Run Python analytics ──────────────────────────────────────────────

def run_analytics() -> dict:
    """
    Call mws_analytics functions directly and return a structured summary dict
    suitable for inclusion in the Claude prompt.
    """
    log.info("Loading system files via mws_analytics...")
    policy, state, hist, hold = mws_analytics.load_system_files()

    log.info("Checking drawdown state...")
    dd = mws_analytics.check_drawdown_state(policy)

    log.info("Running MWS audit (candidate universe)...")
    candidates, _, missing = mws_analytics.run_mws_audit(policy, state, hist, hold)

    log.info("Calculating portfolio value...")
    total_val, asof = mws_analytics.calculate_portfolio_value(policy, hold, hist)

    log.info("Generating momentum rankings...")
    df_scores = mws_analytics.generate_rankings(policy, hist, candidates, hold)

    # Per-ticker execution gate (buy direction as default for reporting)
    gate_rows = []
    gate_cfg = (policy.get("execution_gates", {}) or {}).get("short_term_confirmation", {})
    if gate_cfg.get("enabled", False):
        for ticker in candidates:
            t_hist = hist[hist["Ticker"] == ticker].sort_values("Date")
            if len(t_hist) < 5:
                continue
            result = mws_analytics.check_execution_gate(
                policy=policy,
                ticker=ticker,
                trade_direction="BUY",
                hist=hist,
                stress_active=(dd.get("state") == "soft_limit"),
            )
            gate_rows.append({
                "ticker":       ticker,
                "gate_action":  result.get("action", "UNKNOWN"),
                "z_score":      round(result.get("z_score", 0), 3),
                "vol_clamp":    result.get("vol_clamp_type", "none"),
                "raw_vol_pct":  round(result.get("raw_vol_2d", 0) * 100, 3),
                "eff_vol_pct":  round(result.get("effective_vol_2d", 0) * 100, 3),
            })
    df_gates = pd.DataFrame(gate_rows) if gate_rows else pd.DataFrame()

    return {
        "policy":          policy,
        "state":           state,
        "holdings":        hold,
        "hist":            hist,       # kept for portfolio table MV calculations
        "drawdown":        dd,
        "total_val":       total_val,
        "val_asof":        asof,
        "candidates":      candidates,
        "missing_hist":    missing,
        "df_scores":       df_scores,
        "df_gates":        df_gates,
    }


# ── Step 2: Build Claude prompt ───────────────────────────────────────────────

def build_prompt(analytics: dict) -> str:
    policy  = analytics["policy"]
    state   = analytics["state"]
    hold    = analytics["holdings"]
    dd      = analytics["drawdown"]
    scores  = analytics["df_scores"]
    gates   = analytics["df_gates"]

    # Trim policy for prompt — exclude bulky sections not needed by the LLM runner
    policy_trimmed = {k: v for k, v in policy.items() if k not in [
        "ticker_constraints",   # very long; not needed for recommendation
    ]}

    scores_str = scores.to_string(index=False) if not scores.empty else "No rankings generated."
    gates_str  = gates.to_string(index=False)  if not gates.empty  else "Execution gate disabled or no data."
    hold_str   = hold.to_csv(index=False)
    state_str  = json.dumps(state, indent=2)

    macro_text = ""
    try:
        with open(MACRO_FILE) as f:
            macro_text = f.read()
    except FileNotFoundError:
        macro_text = f"[{MACRO_FILE} not found]"

    return f"""You are the MWS (Momentum-Weighted Scaling) portfolio runner. Today is {TODAY}.
This run was triggered by: **{TRIGGER_REASON}**.

**BREVITY RULE — READ FIRST:** The `<mws_recommendation>` block is a daily email brief.
It must fit comfortably on one screen. Omit any section that has nothing to report.
Do not explain methodology, repeat policy rules, or narrate your reasoning in the email.
State conclusions only. Save all detailed analysis for `<mws_market_context>`.

Execute the full MWS run protocol (Steps 1–8 from mws_policy.json → news_intelligence.run_protocol):

**STEP 1 (FIRST):** Use the web_search tool to search for current news across all 8 categories in
news_intelligence.categories. Use the exact search queries in news_intelligence.generation_protocol.search_queries.
Rate each item HIGH / MEDIUM / LOW per materiality_scale rules. Save full detail in mws_market_context.

**STEPS 2–8:** Using the inputs below, compute sleeve targets, apply execution gate, assess news overlay,
identify override candidates. Put all reasoning in `<mws_market_context>`. Put only conclusions in `<mws_recommendation>`.

Structure your response using these EXACT XML tags (do not rename or omit them):

<mws_market_context>
# MWS Market Context — AUTO-GENERATED {TODAY}
_Generated by LLM runner. Do not edit manually. Overwritten each run._

## 1. Macro / Rates / Inflation
## 2. Geopolitical / Commodity
## 3. AI / Semiconductor
## 4. Energy / Materials
## 5. Crypto / Regulatory
## 6. Biotech / FDA
## 7. Rates / Currency
## 8. Cross-Asset / Technical
[Each section: list items found with materiality rating and CONFIRMS/CONTRADICTS/NOVEL label per sleeve.]
[If no items found: "NO DATA — web search unavailable"]

## Override Candidates
[HIGH-materiality CONTRADICTS items only: ticker | sleeve | signal | news implication | recommendation]
[If none: "None."]

## Market Regime Snapshot
[VIX level + 5-day trend. Drawdown state + current pct from peak. 1–2 sentence regime assessment.]
</mws_market_context>

<mws_recommendation>
# MWS Brief — {TODAY} — {TRIGGER_REASON}

**TPV:** $[amount] | **Drawdown:** [state] ([pct]% from peak)

## Trades
[Bulleted list — one line each: TICKER BUY/SELL X shares ~$Y — [gate status if deferred]]
[If no trades: "No trades — all positions within bands."]

## Signal Changes
[Only tickers with rank change, floor exit, re-entry, or gate flag — one line each]
[Omit if no changes]

## News Flags
[2–4 bullets max — HIGH items only that affect a trade or trigger override review]
[Omit if nothing HIGH materiality]

## Watch
[1–3 bullets — deferred trade countdowns, threshold proximity, upcoming catalysts]
[Omit if nothing actionable]

## Policy Alert
[Only if: compliance breach, proposed rule change, or hard_limit event. Omit otherwise.]
</mws_recommendation>

==============================================================================
## INPUTS
==============================================================================

### POLICY (mws_policy.json — ticker_constraints omitted for brevity)
```json
{json.dumps(policy_trimmed, indent=2, ensure_ascii=False)}
```

### GOVERNANCE RATIONALE (mws_governance.md)
{macro_text}

### CURRENT HOLDINGS (mws_holdings.csv)
```
{hold_str}
```

### SYSTEM STATE (mws_tracker.json)
```json
{state_str}
```

### PORTFOLIO VALUE & DRAWDOWN
- Total Portfolio Value: ${analytics['total_val']:,.2f} (as of {analytics['val_asof']})
- Drawdown State: {dd['state'].upper()} | Current drawdown: {abs(dd.get('drawdown', 0)) * 100:.1f}%

### MOMENTUM RANKINGS (computed by mws_analytics.py)
```
{scores_str}
```

### EXECUTION GATE RESULTS (per-ticker, BUY direction)
```
{gates_str}
```

### MISSING FROM HISTORY
{analytics['missing_hist'] if analytics['missing_hist'] else 'None'}
"""


# ── Step 3: Call Claude API ───────────────────────────────────────────────────

def call_claude(prompt: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable not set.")

    client = anthropic.Anthropic(api_key=api_key)

    log.info("Calling Claude API (model=%s, max_tokens=%d)...", MODEL, MAX_TOKENS)

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    # Concatenate all text blocks (tool results are interleaved but text is what we want)
    full_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            full_text += block.text

    log.info("Claude response: %d chars, stop_reason=%s", len(full_text), response.stop_reason)
    return full_text


# ── Schema violation sentinel ─────────────────────────────────────────────────

class SchemaViolationError(Exception):
    """Raised when the LLM response fails structural validation.
    Triggers fail-closed behavior: no recommendation email, alert email only.
    """


# ── Step 4: Parse and write mws_market_context.md ────────────────────────────

def validate_schema(text: str) -> list[str]:
    """
    Field-level schema validation (defense-in-depth, OWASP LLM02 + LLM04):
    - Each required block must appear exactly once.
    - Total response and each block must be within size bounds.
    - No text may appear outside the two XML blocks (Test 3 / schema-adversary fix).
    Returns a list of violation strings (empty = clean).
    """
    violations = []
    required_tags = ["mws_market_context", "mws_recommendation"]

    # Size guard — defend against model DoS via huge output (OWASP LLM04)
    if len(text) > MAX_RESPONSE_CHARS:
        violations.append(
            f"Response size {len(text):,} chars exceeds MAX_RESPONSE_CHARS={MAX_RESPONSE_CHARS:,}"
        )

    for tag in required_tags:
        pattern = rf"<{tag}>(.*?)</{tag}>"
        matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
        if len(matches) == 0:
            violations.append(f"<{tag}> block MISSING")
        elif len(matches) > 1:
            violations.append(f"<{tag}> block appears {len(matches)} times (must be exactly 1)")
        else:
            block_len = len(matches[0])
            if block_len > MAX_BLOCK_CHARS:
                violations.append(
                    f"<{tag}> block size {block_len:,} chars exceeds MAX_BLOCK_CHARS={MAX_BLOCK_CHARS:,}"
                )

    # Outside-tags check (Test 3 schema-adversary defense — OWASP LLM02):
    # Strip all content inside the two required XML blocks, then check
    # whether any non-whitespace content remains outside them.
    stripped = text
    for tag in required_tags:
        stripped = re.sub(rf"<{tag}>.*?</{tag}>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    outside_text = stripped.strip()
    if outside_text:
        preview = outside_text[:200].replace("\n", " ")
        violations.append(
            f"Text found outside XML blocks ({len(outside_text):,} chars). "
            f"Preview: '{preview}'"
        )

    return violations


def repair_schema(text: str) -> tuple:
    """
    Attempt surgical repairs of common LLM schema violations before failing closed.
    Returns (repaired_text, list_of_repairs_applied).

    Repair sequence (order matters — aliases must be normalized first):
      1. Tag alias normalization  (<market_context> → <mws_market_context>, etc.)
      2. Missing closing tag insertion (open tag present, close absent)
      3. Preamble stripping  (text before the first required opening tag)
      4. Postamble stripping (text after the last required closing tag)
    """
    REQUIRED_TAGS: list = ["mws_market_context", "mws_recommendation"]
    # Map of known alias → canonical tag name.  Only applied when the canonical
    # tag is *absent* — prevents double-rename if both are somehow present.
    ALIASES: dict = {
        "market_context": "mws_market_context",
        "mws_context":    "mws_market_context",
        "recommendation": "mws_recommendation",
        "mws_rec":        "mws_recommendation",
    }

    repairs: list = []
    result: str = text

    # ── 1. Normalize tag aliases ───────────────────────────────────────────────
    for alias, canonical in ALIASES.items():
        if not re.search(rf"<{re.escape(canonical)}\b", result, re.IGNORECASE):
            if re.search(rf"<{re.escape(alias)}\b", result, re.IGNORECASE):
                before = result
                result = re.sub(
                    rf"<({re.escape(alias)})(\s*/?)\s*>",
                    f"<{canonical}\\2>",
                    result,
                    flags=re.IGNORECASE,
                )
                result = re.sub(
                    rf"</{re.escape(alias)}\s*>",
                    f"</{canonical}>",
                    result,
                    flags=re.IGNORECASE,
                )
                if result != before:
                    repairs.append(f"<{alias}> → <{canonical}>")

    # ── 2. Append missing closing tags ────────────────────────────────────────
    for tag in REQUIRED_TAGS:
        has_open  = bool(re.search(rf"<{re.escape(tag)}\b[^>]*>",  result, re.IGNORECASE))
        has_close = bool(re.search(rf"</{re.escape(tag)}\s*>",     result, re.IGNORECASE))
        if has_open and not has_close:
            result += f"\n</{tag}>"
            repairs.append(f"Appended missing </{tag}>")

    # ── 3. Strip preamble (text before first required opening tag) ────────────
    open_positions = []
    for tag in REQUIRED_TAGS:
        m = re.search(rf"<{re.escape(tag)}\b[^>]*>", result, re.IGNORECASE)
        if m:
            open_positions.append(m.start())
    if open_positions:
        first_open = min(open_positions)
        if first_open > 0:
            preamble = result[:first_open].strip()
            if preamble:
                preview = preamble[:80].replace("\n", " ")
                repairs.append(
                    f"Stripped {len(preamble)}-char preamble: '{preview}...'"
                )
                result = result[first_open:]

    # ── 4. Strip postamble (text after last required closing tag) ─────────────
    close_positions = []
    for tag in REQUIRED_TAGS:
        for m in re.finditer(rf"</{re.escape(tag)}\s*>", result, re.IGNORECASE):
            close_positions.append(m.end())
    if close_positions:
        last_close = max(close_positions)
        if last_close < len(result):
            postamble = result[last_close:].strip()
            if postamble:
                preview = postamble[:80].replace("\n", " ")
                repairs.append(
                    f"Stripped {len(postamble)}-char postamble: '{preview}...'"
                )
                result = result[:last_close]

    return result, repairs


def extract_section(text: str, tag: str) -> str:
    """Extract content between <tag> ... </tag> XML-style markers (case-insensitive, dotall)."""
    pattern = rf"<{tag}>(.*?)</{tag}>"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if not match:
        log.warning(
            "SCHEMA VIOLATION: <%s> block missing from LLM response. "
            "Response length: %d chars. First 500 chars: %s",
            tag, len(text), text[:500],
        )
    return match.group(1).strip() if match else ""

def write_market_context(response_text: str) -> None:
    """Write the market context file — raises SchemaViolationError if response is malformed.

    Fail-closed: any structural violation halts execution before the recommendation
    email is sent. The violation report is written to disk for audit, then the
    exception propagates to main() which sends an alert email and exits(1).
    """
    violations = validate_schema(response_text)
    if violations:
        for v in violations:
            log.error("SCHEMA VIOLATION: %s", v)
        # Write violation report to disk for audit trail
        report = (
            f"# MWS Market Context — SCHEMA VIOLATION — {TODAY}\n\n"
            "**This run failed output schema validation. No trades were recommended.**\n\n"
            "## Violations\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n\nCheck GitHub Actions logs for the raw LLM response.\n"
        )
        with open(MARKET_CTX_FILE, "w", encoding="utf-8") as f:
            f.write(report)
        raise SchemaViolationError(
            f"{len(violations)} schema violation(s): {'; '.join(violations)}"
        )

    content = extract_section(response_text, "mws_market_context")
    # At this point validate_schema() already confirmed the block exists, so
    # content will be non-empty; the fallback is a defensive belt-and-suspenders.
    if not content:
        content = (
            f"# MWS Market Context — AUTO-GENERATED {TODAY}\n\n"
            "[context block present but empty — check runner logs]\n"
        )

    with open(MARKET_CTX_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    log.info("Wrote %s (%d chars)", MARKET_CTX_FILE, len(content))


# ── Step 5: Send email ────────────────────────────────────────────────────────

_EMAIL_CSS = """
<style>
  body  { font-family: Arial, sans-serif; font-size: 13px; color: #222; max-width: 960px; margin: 0 auto; }
  h1    { font-size: 20px; color: #111; border-bottom: 2px solid #ddd; padding-bottom: 4px; }
  h2    { font-size: 16px; color: #222; margin-top: 20px; border-bottom: 1px solid #eee; }
  h3    { font-size: 14px; color: #333; margin-top: 14px; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0 16px; }
  th    { background: #f0f4f8; border: 1px solid #ccc; padding: 6px 10px; text-align: left; }
  td    { border: 1px solid #ddd; padding: 5px 10px; }
  tr:nth-child(even) td { background: #fafafa; }
  hr    { border: none; border-top: 1px solid #ddd; margin: 16px 0; }
  code  { background: #f5f5f5; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
  strong { color: #111; }
  blockquote { border-left: 3px solid #ccc; margin: 8px 0; padding: 4px 12px; color: #555; }
</style>
"""

def _md_to_fragment(text: str) -> str:
    """Convert markdown to an HTML fragment (no <html>/<body> wrapper)."""
    return md.markdown(text, extensions=["tables", "nl2br", "fenced_code"])


def _to_html(text: str) -> str:
    """Convert markdown to styled HTML suitable for email (full document)."""
    return f"<html><head>{_EMAIL_CSS}</head><body>{_md_to_fragment(text)}</body></html>"


def _df_to_md_table(df: pd.DataFrame) -> str:
    """Convert a DataFrame to a markdown table without requiring tabulate."""
    if df.empty:
        return "_No data._"
    cols = list(df.columns)
    header = "| " + " | ".join(str(c) for c in cols) + " |"
    sep    = "|" + "|".join([":---"] * len(cols)) + "|"
    rows   = [
        "| " + " | ".join(str(v) for v in row) + " |"
        for _, row in df.iterrows()
    ]
    return "\n".join([header, sep] + rows)


def _build_portfolio_tables(analytics: dict) -> str:
    """
    Build a unified portfolio-state HTML table for the email body.

    One table, hierarchically grouped: Portfolio → L1 (sorted by $MV desc) →
    L2 (sorted by $MV desc within L1) → Ticker (sorted by momentum rank desc).

    Columns: L1 / L2 | Ticker | Rank% | α/VTI | Wt% | $MV | Action | Gate z

    Action colour coding:
      BUY        → green   (#c8e6c9)
      DEFER-BUY  → amber   (#ffe082)
      TRIM       → red     (#ffcdd2)
      HOLD       → yellow  (#fff9c4)
      SPIKE-TRIM → teal    (#b2ebf2)  – sell into strength

    Action derivation (per ticker):
      • Sleeve below floor            → BUY  (sleeve needs capital)
      • Sleeve above cap              → TRIM (sleeve over-allocated)
      • In-band, ticker Pct ≥ 0.65   → BUY  (strong momentum, within-sleeve add)
      • In-band, ticker Pct ≤ 0.30   → TRIM (weak momentum, within-sleeve trim)
      • Everything else               → HOLD
      • gate_action == "defer"        → BUY → DEFER-BUY
      • gate_action == "spike_trim"   → SPIKE-TRIM (overrides all)

    Returns an HTML string (no <html>/<body> wrapper).
    """
    # ── Inline style constants ────────────────────────────────────────────────
    _TH = (
        "background:#f0f4f8; border:1px solid #bcd; padding:6px 10px; "
        "text-align:left; white-space:nowrap; font-size:12px;"
    )
    _TD  = "border:1px solid #ddd; padding:5px 8px; white-space:nowrap; font-size:12px;"
    _TDR = "border:1px solid #ddd; padding:5px 8px; text-align:right; white-space:nowrap; font-size:12px;"

    try:
        policy    = analytics["policy"]
        hold      = analytics["holdings"].copy()
        hist      = analytics["hist"]
        total_val = analytics["total_val"]
        val_asof  = analytics["val_asof"]
        dd        = analytics["drawdown"]
        df_scores = analytics["df_scores"]
        df_gates  = analytics["df_gates"]

        # ── Per-ticker market value ───────────────────────────────────────────
        fixed_raw = (policy.get("governance", {}) or {}).get("fixed_asset_prices", {}) or {}
        latest_px = hist.sort_values("Date").groupby("Ticker")["AdjClose"].last()

        def _get_price(ticker: str) -> float:
            entry = fixed_raw.get(ticker)
            if entry is not None:
                if isinstance(entry, dict):
                    lp = latest_px.get(ticker)
                    if lp is not None and float(lp) > 0:
                        return float(lp)
                    return float(entry.get("fallback_price", 0))
                try:
                    return float(entry)
                except (TypeError, ValueError):
                    return 0.0
            lp = latest_px.get(ticker)
            return float(lp) if lp is not None else 0.0

        hold["Price"] = hold["Ticker"].map(_get_price)
        hold["MV"]    = hold["Shares"] * hold["Price"]
        held_tickers  = set(hold["Ticker"].tolist())

        # ── Allocatable denominator ───────────────────────────────────────────
        overlay_mv  = hold.loc[hold["Class"] == "managed_futures", "MV"].sum()
        bucket_a_mv = hold.loc[hold["Class"] == "bucket_a",        "MV"].sum()
        alloc_denom = total_val - overlay_mv - bucket_a_mv

        # ── Lookup dicts: momentum + gate ─────────────────────────────────────
        scores_by_ticker: dict = {}
        if not df_scores.empty:
            for _, row in df_scores.iterrows():
                scores_by_ticker[row["Ticker"]] = {
                    "pct":   float(row["Pct"]) if pd.notna(row["Pct"]) else 0.0,
                    "alpha": str(row.get("Alpha", "—")),
                }

        gates_by_ticker: dict = {}
        if df_gates is not None and not df_gates.empty:
            for _, row in df_gates.iterrows():
                gates_by_ticker[str(row["ticker"])] = {
                    "action":  str(row.get("gate_action", "proceed")),
                    "z_score": row.get("z_score"),
                }

        # ── Policy sleeve layout ──────────────────────────────────────────────
        sleeves_l1 = policy["sleeves"]["level1"]
        sleeves_l2 = policy["sleeves"]["level2"]

        # ── Per-sleeve $MV totals ─────────────────────────────────────────────
        def _l2_mv(name: str) -> float:
            return float(hold.loc[hold["Class"] == name, "MV"].sum())

        def _l1_mv(name: str) -> float:
            children = sleeves_l1.get(name, {}).get("children", [])
            return sum(_l2_mv(c) for c in children)

        # ── Action label + background colour per ticker ───────────────────────
        def _action(ticker: str, sleeve_cur_pct: float,
                    floor_pct: float, cap_pct: float) -> tuple:
            gate        = gates_by_ticker.get(ticker, {})
            gate_action = gate.get("action", "proceed")
            pct         = scores_by_ticker.get(ticker, {}).get("pct", 0.5)

            if gate_action == "spike_trim":
                return "SPIKE-TRIM", "#b2ebf2"

            # Base action from sleeve compliance + per-ticker momentum
            if sleeve_cur_pct < floor_pct - 0.1:
                base = "BUY"
            elif sleeve_cur_pct > cap_pct + 0.1:
                base = "TRIM"
            elif pct >= 0.65:
                base = "BUY"
            elif pct <= 0.30:
                base = "TRIM"
            else:
                base = "HOLD"

            if base == "BUY" and gate_action == "defer":
                return "DEFER-BUY", "#ffe082"
            if base == "BUY":
                return "BUY",  "#c8e6c9"
            if base == "TRIM":
                return "TRIM", "#ffcdd2"
            return "HOLD", "#fff9c4"

        # ── Sort L1: largest total $MV first; stabilizers (overlays) always last
        def _l1_sort(name: str) -> float:
            return -1.0 if name == "stabilizers" else _l1_mv(name)

        l1_sorted = sorted(sleeves_l1.keys(), key=_l1_sort, reverse=True)

        # ── Build table rows ──────────────────────────────────────────────────
        tbody_rows: list[str] = []

        for l1_name in l1_sorted:
            l1_data    = sleeves_l1.get(l1_name, {})
            l1_cap     = l1_data.get("cap")
            children   = l1_data.get("children", [])
            is_overlay = (l1_name == "stabilizers")
            denom      = total_val if is_overlay else alloc_denom
            l1_total   = _l1_mv(l1_name)
            l1_cur_pct = (l1_total / denom * 100) if denom > 0 else 0.0
            l1_cap_str = f"{l1_cap * 100:.0f}%" if l1_cap else "overlay"

            # L1 header row (full-width, dark blue-grey)
            tbody_rows.append(
                f'<tr style="background:#d0dff0;">'
                f'<td colspan="8" style="border:1px solid #aac; padding:7px 10px; '
                f'font-weight:bold; font-size:12px;">'
                f'▶&nbsp; {l1_name}'
                f'&ensp;|&ensp;cap: {l1_cap_str}'
                f'&ensp;|&ensp;current: {l1_cur_pct:.1f}%'
                f'&ensp;(${l1_total:,.0f})'
                f'</td></tr>'
            )

            # Sort L2 children by $MV desc
            children_sorted = sorted(children, key=_l2_mv, reverse=True)

            for l2_name in children_sorted:
                l2_data   = sleeves_l2.get(l2_name, {})
                floor_pct = (l2_data.get("floor") or 0) * 100
                cap_pct   = (l2_data.get("cap")   or 0) * 100
                l2_total  = _l2_mv(l2_name)
                cur_pct   = (l2_total / denom * 100) if denom > 0 else 0.0
                tickers   = l2_data.get("tickers", [])

                if   cur_pct < floor_pct - 0.1: l2_status = "⚠️ BELOW FLOOR"
                elif cur_pct > cap_pct   + 0.1: l2_status = "⚠️ ABOVE CAP"
                else:                           l2_status = "✅"

                # L2 subheader row (lighter blue-grey, indented)
                tbody_rows.append(
                    f'<tr style="background:#eef3f9;">'
                    f'<td colspan="8" style="border:1px solid #ccd; padding:5px 10px 5px 26px; '
                    f'font-style:italic; font-size:12px;">'
                    f'&nbsp;&nbsp;{l2_name}'
                    f'&ensp;floor {floor_pct:.0f}% – cap {cap_pct:.0f}%'
                    f'&ensp;|&ensp;current: <strong>{cur_pct:.1f}%</strong>'
                    f'&ensp;(${l2_total:,.0f})'
                    f'&ensp;{l2_status}'
                    f'</td></tr>'
                )

                # Ticker rows — held tickers only, sorted by momentum Pct desc
                held_in_sleeve = [t for t in tickers if t in held_tickers]
                held_in_sleeve.sort(
                    key=lambda t: scores_by_ticker.get(t, {}).get("pct", 0.0),
                    reverse=True,
                )

                for ticker in held_in_sleeve:
                    t_mv  = float(hold.loc[hold["Ticker"] == ticker, "MV"].sum())
                    t_wt  = (t_mv / denom * 100) if denom > 0 else 0.0
                    s     = scores_by_ticker.get(ticker, {})
                    pct   = s.get("pct", 0.0)
                    alpha = s.get("alpha", "—")
                    gate  = gates_by_ticker.get(ticker, {})
                    z_val = gate.get("z_score")
                    z_str = f"{z_val:+.2f}" if z_val is not None else "—"

                    action_label, action_bg = _action(ticker, cur_pct, floor_pct, cap_pct)
                    pct_str = f"{pct * 100:.0f}%" if pct else "—"

                    tbody_rows.append(
                        f'<tr>'
                        f'<td style="{_TD}"></td>'
                        f'<td style="{_TD}"><strong>{ticker}</strong></td>'
                        f'<td style="{_TDR}">{pct_str}</td>'
                        f'<td style="{_TDR}">{alpha}</td>'
                        f'<td style="{_TDR}">{t_wt:.1f}%</td>'
                        f'<td style="{_TDR}">${t_mv:,.0f}</td>'
                        f'<td style="border:1px solid #ddd; padding:5px 8px; text-align:center; '
                        f'font-weight:bold; font-size:12px; background:{action_bg};">'
                        f'{action_label}</td>'
                        f'<td style="{_TDR}">{z_str}</td>'
                        f'</tr>'
                    )

        # ── Bucket A + Cash footer rows ───────────────────────────────────────
        bucket_a_pct = (bucket_a_mv / total_val * 100) if total_val > 0 else 0.0
        tbody_rows.append(
            f'<tr style="background:#f5f5f5;">'
            f'<td style="{_TD}" colspan="2"><em>Bucket A — protected liquidity</em></td>'
            f'<td style="{_TD}">—</td>'
            f'<td style="{_TD}">—</td>'
            f'<td style="{_TDR}">{bucket_a_pct:.1f}%</td>'
            f'<td style="{_TDR}">${bucket_a_mv:,.0f}</td>'
            f'<td style="{_TD}">🔒</td>'
            f'<td style="{_TD}">—</td>'
            f'</tr>'
        )
        cash_mv = float(hold.loc[hold["Class"] == "bucket_b", "MV"].sum())
        if cash_mv > 0:
            cash_pct = (cash_mv / total_val * 100)
            tbody_rows.append(
                f'<tr>'
                f'<td style="{_TD}" colspan="2"><em>Cash (Bucket B)</em></td>'
                f'<td style="{_TD}">—</td>'
                f'<td style="{_TD}">—</td>'
                f'<td style="{_TDR}">{cash_pct:.1f}%</td>'
                f'<td style="{_TDR}">${cash_mv:,.0f}</td>'
                f'<td style="{_TD}">—</td>'
                f'<td style="{_TD}">—</td>'
                f'</tr>'
            )

        # ── Portfolio status header ───────────────────────────────────────────
        dd_state = dd.get("state", "normal").upper()
        dd_pct   = abs(dd.get("drawdown", 0)) * 100
        peak_tpv = dd.get("peak_tpv", total_val)
        status_html = (
            f'<p style="font-size:13px; margin:4px 0;">'
            f'<strong>TPV:</strong> ${total_val:,.0f} (as of {val_asof})'
            f'&ensp;|&ensp;<strong>Drawdown:</strong> {dd_state} — {dd_pct:.1f}% from peak'
            f' (${peak_tpv:,.0f})<br>'
            f'<strong>Allocatable denominator:</strong> ${alloc_denom:,.0f}'
            f' (TPV − overlays − Bucket A)</p>'
        )

        # ── Column headers ────────────────────────────────────────────────────
        col_headers = ["L1 / L2 Sleeve", "Ticker", "Rank", "α / VTI", "Wt %", "$MV", "Action", "Gate z"]
        thead = (
            "<thead><tr>"
            + "".join(f'<th style="{_TH}">{c}</th>' for c in col_headers)
            + "</tr></thead>"
        )

        table = (
            f'<table style="border-collapse:collapse; width:100%; margin:8px 0 16px;">'
            f'{thead}'
            f'<tbody>{"".join(tbody_rows)}</tbody>'
            f'</table>'
        )

        # ── Colour legend ─────────────────────────────────────────────────────
        legend = (
            '<p style="font-size:11px; color:#555; margin:0 0 12px;">'
            '<span style="background:#c8e6c9; padding:1px 6px; border-radius:3px;">BUY</span>&ensp;'
            '<span style="background:#ffe082; padding:1px 6px; border-radius:3px;">DEFER-BUY</span>&ensp;'
            '<span style="background:#fff9c4; padding:1px 6px; border-radius:3px;">HOLD</span>&ensp;'
            '<span style="background:#ffcdd2; padding:1px 6px; border-radius:3px;">TRIM</span>&ensp;'
            '<span style="background:#b2ebf2; padding:1px 6px; border-radius:3px;">SPIKE-TRIM</span>'
            '&ensp; | Action = sleeve floor/cap compliance + per-ticker momentum rank; '
            'Gate z = EWMA vol-scaled 2-day z-score'
            '</p>'
        )

        return status_html + table + legend

    except Exception as tbl_err:
        log.warning("Portfolio table generation failed: %s", tbl_err, exc_info=True)
        return f'<p><em>Portfolio state table unavailable: {tbl_err}</em></p>'


def send_schema_alert(violation_summary: str) -> None:
    """Send a schema-violation alert email. Called instead of send_email() on fail-closed path."""
    password  = os.environ.get("GMAIL_APP_PASSWORD")
    from_addr = os.environ.get("GMAIL_FROM", "bhatnagar.vivek@gmail.com")
    to_addr   = os.environ.get("GMAIL_TO",   "bhatnagar.vivek@gmail.com")

    if not password:
        log.warning("GMAIL_APP_PASSWORD not set — schema alert not emailed (check logs).")
        return

    body_text = (
        f"MWS Run FAILED schema validation on {TODAY}.\n\n"
        f"Violations:\n{violation_summary}\n\n"
        "No trade recommendation was produced. No portfolio action should be taken.\n"
        "Check GitHub Actions logs for the raw LLM response."
    )
    body_html = _to_html(
        f"# ⚠️ MWS Schema Violation — {TODAY}\n\n"
        f"**Run failed output schema validation. No recommendation produced.**\n\n"
        f"## Violations\n{violation_summary}\n\n"
        "Check GitHub Actions logs for raw LLM response. No portfolio action should be taken."
    )

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"⚠️ MWS SCHEMA VIOLATION — {TODAY} — ACTION REQUIRED"
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(body_text, "plain", "utf-8"))
    body.attach(MIMEText(body_html, "html",  "utf-8"))
    msg.attach(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, password)
        smtp.sendmail(from_addr, to_addr, msg.as_string())

    log.info("Schema alert email sent to %s", to_addr)


def send_email(response_text: str, analytics: dict) -> None:
    password  = os.environ.get("GMAIL_APP_PASSWORD")
    from_addr = os.environ.get("GMAIL_FROM", "bhatnagar.vivek@gmail.com")
    to_addr   = os.environ.get("GMAIL_TO",   "bhatnagar.vivek@gmail.com")

    if not password:
        log.warning("GMAIL_APP_PASSWORD not set — skipping email. Output committed to repo.")
        return

    recommendation = extract_section(response_text, "mws_recommendation")
    if not recommendation:
        # Schema violation — send a clear alert rather than the raw (potentially huge) response
        recommendation = (
            f"# ⚠️ MWS Run — SCHEMA VIOLATION — {TODAY}\n\n"
            "The LLM response did not contain a valid `<mws_recommendation>` block.\n"
            "This run failed output schema validation.\n\n"
            "**Action required:** Check GitHub Actions logs for the raw LLM response.\n"
        )
        log.warning("SCHEMA VIOLATION: <mws_recommendation> block missing — sending alert email.")

    market_context = extract_section(response_text, "mws_market_context")

    subject = f"MWS Run — {TODAY} — {TRIGGER_REASON}"

    chart_path = mws_analytics.CHART_FILENAME
    has_chart  = os.path.exists(chart_path)
    chart_cid  = "mws_equity_curve"

    # ── Build combined email body ─────────────────────────────────────────────
    # Layout (top → bottom):
    #   1. <mws_recommendation>  — executive summary / actionable brief  (markdown → HTML)
    #   2. Portfolio state table — Python-computed unified HTML table (HTML passthrough)
    #   3. <mws_market_context>  — full LLM market analysis              (markdown → HTML)
    #   4. Chart                 — inline CID image at bottom

    rec_html = _md_to_fragment(recommendation)
    portfolio_html = _build_portfolio_tables(analytics)
    ctx_html = _md_to_fragment(market_context) if market_context else ""

    body_content = rec_html
    body_content += '<hr><h2>Portfolio State</h2>' + portfolio_html
    if ctx_html:
        body_content += '<hr>' + ctx_html

    html_body = f"<html><head>{_EMAIL_CSS}</head><body>{body_content}</body></html>"
    if has_chart:
        chart_tag = (
            f'<hr style="margin-top:24px">'
            f'<img src="cid:{chart_cid}" style="max-width:100%;height:auto;" '
            f'alt="MWS Equity Curve">'
        )
        html_body = html_body.replace("</body>", f"{chart_tag}</body>")

    # MIME structure for inline image:
    #   mixed
    #     alternative
    #       text/plain
    #       related          ← only when chart present
    #         text/html      ← references cid:mws_equity_curve
    #         image/png      ← Content-ID + inline disposition
    #     (no attachment)
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(recommendation, "plain", "utf-8"))

    if has_chart:
        related = MIMEMultipart("related")
        related.attach(MIMEText(html_body, "html", "utf-8"))
        with open(chart_path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", f"<{chart_cid}>")
        img.add_header("Content-Disposition", "inline", filename=chart_path)
        related.attach(img)
        alt.attach(related)
        log.info("Chart embedded inline: %s", chart_path)
    else:
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        log.warning("Chart not found, sending without chart: %s", chart_path)

    msg.attach(alt)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, password)
        smtp.sendmail(from_addr, to_addr, msg.as_string())

    log.info("Email sent to %s", to_addr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== MWS GitHub Runner start | %s | trigger=%s ===", TODAY, TRIGGER_REASON)

    try:
        analytics = run_analytics()
        log.info("Analytics complete — %d candidates, TPV $%.0f",
                 len(analytics["candidates"]), analytics["total_val"])

        # Generate equity curve chart (saved to mws_equity_curve.png)
        try:
            mws_charts.rotate_and_chart(analytics["df_scores"], analytics["policy"])
            log.info("Chart generated: %s", mws_analytics.CHART_FILENAME)
        except Exception as chart_err:
            log.warning("Chart generation skipped: %s", chart_err)

        prompt = build_prompt(analytics)
        log.info("Prompt built (%d chars)", len(prompt))

        response = call_claude(prompt)

        violations = validate_schema(response)
        if violations:
            log.warning(
                "Schema violations (%d) — attempting repair before fail-closed: %s",
                len(violations), "; ".join(violations),
            )
            response, repairs = repair_schema(response)
            if repairs:
                log.info("Repairs applied (%d): %s", len(repairs), "; ".join(repairs))
            else:
                log.info("No repairs applicable.")
            # Re-validate after repair; write_market_context() will raise if still bad.
            post_violations = validate_schema(response)
            if post_violations:
                log.error(
                    "Schema violations persist after repair (%d): %s",
                    len(post_violations), "; ".join(post_violations),
                )
            else:
                log.info("Schema valid after repair.")

        write_market_context(response)   # raises SchemaViolationError if still invalid
        send_email(response, analytics)

        log.info("=== MWS GitHub Runner complete ===")

    except SchemaViolationError as sve:
        # Fail-closed: violation report already written to disk by write_market_context().
        # Send alert email (no recommendation), then exit non-zero so GitHub Actions
        # marks the run as FAILED and the user is notified.
        log.error("FAIL-CLOSED: schema violation — %s", sve)
        log.error("Violation report written to: %s", os.path.abspath(MARKET_CTX_FILE))
        try:
            send_schema_alert(str(sve))
        except Exception as alert_err:
            log.error("Failed to send schema alert email: %s", alert_err)
            log.error("Check violation report manually at: %s", os.path.abspath(MARKET_CTX_FILE))
        sys.exit(1)

    except Exception as e:
        log.error("Runner failed: %s", e)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
