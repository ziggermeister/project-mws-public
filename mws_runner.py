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
import time as _time
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
POLICY_FILE              = mws_analytics.POLICY_FILENAME
HOLDINGS_FILE            = mws_analytics.HOLDINGS_CSV
HISTORY_FILE             = mws_analytics.HISTORY_CSV
MACRO_FILE               = mws_analytics.MACRO_MD
MARKET_CTX_FILE          = mws_analytics.MARKET_CTX_MD
RESULTS_FILE             = mws_analytics.RESULTS_CSV
POLICY_RUNTIME_FILE      = mws_analytics.POLICY_RUNTIME_JSON      # stripped policy (LLM use)
PRECOMPUTED_TARGETS_FILE = mws_analytics.PRECOMPUTED_TARGETS_JSON  # trade table (LLM use)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mws_runner")

# ── Run-level telemetry ────────────────────────────────────────────────────────
# _RUN_TIMINGS: wall-clock seconds per phase (populated in main())
# _TOKEN_USAGE: LLM token counts from the API response (populated in call_claude())
_RUN_TIMINGS: dict = {}
_TOKEN_USAGE: dict = {}


# ── Step 1: Run Python analytics ──────────────────────────────────────────────

def run_analytics() -> dict:
    """
    Call mws_analytics functions directly and return a structured summary dict
    suitable for inclusion in the Claude prompt.
    """
    log.info("Loading system files via mws_analytics...")
    policy, hist, hold = mws_analytics.load_system_files()

    log.info("Checking drawdown state...")
    dd = mws_analytics.check_drawdown_state(policy)

    log.info("Running MWS audit (candidate universe)...")
    candidates, _, missing = mws_analytics.run_mws_audit(policy, hist, hold)

    log.info("Calculating portfolio value...")
    total_val, asof = mws_analytics.calculate_portfolio_value(policy, hold, hist)

    log.info("Updating performance log...")
    try:
        mws_analytics.update_performance_log(policy, hist, hold, today_total_val=total_val)
        # Re-read drawdown state now that the log is fresh
        dd = mws_analytics.check_drawdown_state(policy)
    except Exception as perf_err:
        log.warning("Performance log update failed (non-fatal): %s", perf_err)

    log.info("Generating momentum rankings...")
    df_scores = mws_analytics.generate_rankings(policy, hist, candidates, hold)
    mws_analytics.generate_policy_runtime(policy)   # regenerate stripped policy for LLM

    # Per-ticker execution gate (buy direction as default for reporting)
    gate_rows = []
    gate_cfg = (policy.get("execution_gates", {}) or {}).get("short_term_confirmation", {})
    if gate_cfg.get("enabled", False):
        for ticker in candidates:
            t_hist = hist[[ticker]].dropna() if ticker in hist.columns else pd.DataFrame()
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
    hold    = analytics["holdings"]
    dd      = analytics["drawdown"]
    scores  = analytics["df_scores"]
    gates   = analytics["df_gates"]

    # Use the auto-generated runtime policy (stripped of notes/rationale, ~65% smaller).
    # POLICY_RUNTIME_FILE is written by generate_policy_runtime() during run_analytics()
    # so it is always fresh.  Fall back to full policy minus ticker_constraints if missing.
    try:
        with open(POLICY_RUNTIME_FILE, encoding="utf-8") as _rf:
            policy_trimmed = json.load(_rf)
    except Exception:
        policy_trimmed = {k: v for k, v in policy.items() if k not in [
            "ticker_constraints", "objectives", "news_intelligence", "definitions",
        ]}

    scores_str = scores.to_string(index=False) if not scores.empty else "No rankings generated."
    gates_str  = gates.to_string(index=False)  if not gates.empty  else "Execution gate disabled or no data."
    hold_str   = hold.to_csv(index=False)

    # Governance summary — full rationale in mws_governance.md; only conclusions sent to LLM
    macro_text = (
        "MWS v2.9.9 — Python owns all deterministic math (targets, compliance, gates); "
        "LLM role is news overlay, regime assessment, and anomaly flagging only. "
        "Active mechanisms: breadth-conditioned ai_tech floor (22% strong / 12% weak breadth), "
        "bifurcated denominators (sizing_denom excludes overlays+BucketA; compliance_denom also "
        "excludes tactical_cash), absolute momentum filter (Pct≥0.65 AND RawScore≤0 blocks buys), "
        "compliance buy turnover cap (20%/event, Priority-1 hard_limit exempt). "
        "Risk: soft_limit=22% freezes new buys; hard_limit=30% reduces all to floors; "
        "recovery requires <15% drawdown for 10 consecutive days OR VTI positive momentum 5 days."
    )

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

    # ── Capture token usage for benchmark reporting ───────────────────────────
    if hasattr(response, "usage") and response.usage:
        u = response.usage
        _TOKEN_USAGE.update({
            "input_tokens":        getattr(u, "input_tokens",                0) or 0,
            "output_tokens":       getattr(u, "output_tokens",               0) or 0,
            "cache_read_tokens":   getattr(u, "cache_read_input_tokens",     0) or 0,
            "cache_create_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        })
        log.info(
            "⚙️  Token usage: input=%d  output=%d  cache_read=%d  cache_create=%d",
            _TOKEN_USAGE["input_tokens"], _TOKEN_USAGE["output_tokens"],
            _TOKEN_USAGE["cache_read_tokens"], _TOKEN_USAGE["cache_create_tokens"],
        )

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
<meta charset="utf-8">
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
    rows   = (
        df.apply(lambda row: "| " + " | ".join(str(v) for v in row) + " |", axis=1)
        .tolist()
    )
    return "\n".join([header, sep] + rows)


def _build_portfolio_tables(analytics: dict) -> str:
    """
    Build two HTML tables for the email body:

    Table 1 — Portfolio Positions (full hierarchy, informational)
      Grouped: L1 → L2 → Ticker (each sorted by $MV desc; tickers by rank desc)
      Columns: Sleeve / Ticker | Rank | Alpha vs VTI | Weight | Market Value

    Table 2 — Recommended Actions (only non-HOLD tickers)
      Columns: Ticker | Sleeve | Rank | Action | Basis | Est. Trade | Est. Shares | Gate z

    Action logic (per ticker):
      Sleeve below floor  → BUY  (compliance)
      Sleeve above cap    → TRIM (compliance)
      In-band, rank ≤ 30% → TRIM (momentum reallocation)
      In-band, rank ≥ 65% → BUY  (momentum reallocation)
      Otherwise           → HOLD
      gate = defer + BUY  → DEFER-BUY
      gate = spike_trim   → SPIKE-TRIM (overrides all)

    Trade size:
      Compliance BUY  (~): (floor − current) × denom ÷ n_buy_tickers_in_sleeve
      Compliance TRIM (~): (current − cap)   × denom × ticker_mv / total_trim_mv_in_sleeve
      Momentum BUY/TRIM (≈): abs(target_mv − current_mv) where target_mv is
        ticker's momentum-pct share of l2 total; higher uncertainty, shown with ≈
    """
    _TH  = ("background:#f0f4f8; border:1px solid #bcd; padding:6px 10px; "
            "text-align:left; white-space:nowrap; font-size:12px;")
    _THR = ("background:#f0f4f8; border:1px solid #bcd; padding:6px 10px; "
            "text-align:right; white-space:nowrap; font-size:12px;")
    _TD  = "border:1px solid #ddd; padding:5px 8px; white-space:nowrap; font-size:12px;"
    _TDR = "border:1px solid #ddd; padding:5px 8px; text-align:right; white-space:nowrap; font-size:12px;"
    _TDC = "border:1px solid #ddd; padding:5px 8px; text-align:center; white-space:nowrap; font-size:12px;"

    try:
        policy    = analytics["policy"]
        hold      = analytics["holdings"].copy()
        hist      = analytics["hist"]
        total_val = analytics["total_val"]
        val_asof  = analytics["val_asof"]
        dd        = analytics["drawdown"]
        df_scores = analytics["df_scores"]
        df_gates  = analytics["df_gates"]

        # ── Per-ticker price / MV ─────────────────────────────────────────────
        fixed_raw = (policy.get("governance", {}) or {}).get("fixed_asset_prices", {}) or {}
        latest_px = hist.iloc[-1]

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

        # ── Bifurcated denominators — tactical cash management (v2.9.8) ──────
        # sizing_denom    = full deployable pie; cash stays visible so recovery
        #                   buys scale correctly when the abs-momentum filter lifts.
        # compliance_denom = sizing_denom minus tactical cash; used ONLY for
        #                   L1/L2 floor/cap breach detection so blocked-filter
        #                   cash doesn't trigger spurious compliance buys.
        _cash_val = float(hold.loc[hold["Ticker"] == "CASH", "MV"].sum()) \
                    if "CASH" in held_tickers else 0.0
        sizing_denom     = alloc_denom
        compliance_denom = alloc_denom
        _tactical_cash   = 0.0
        _tcm = policy.get("tactical_cash_management", {})
        if _tcm.get("enabled", False):
            _buf    = total_val * float(_tcm.get("cash_reserve_buffer_pct", 0.01))
            _raw_tc = max(0.0, _cash_val - _buf)
            _tcs: dict = {}
            _tcs_path = mws_analytics.TACTICAL_CASH_STATE_JSON
            if os.path.exists(_tcs_path):
                try:
                    with open(_tcs_path, "r", encoding="utf-8") as _f:
                        _tcs = json.load(_f)
                except Exception:
                    pass
            _blocking = _tcs.get("filter_blocking", False)
            _consec   = int(_tcs.get("consecutive_blocked_days", 0))
            _persist  = int(_tcm.get("persistence_required_days", 2))
            if _blocking and _consec >= _persist and _raw_tc > 0:
                _cap           = total_val * float(_tcm.get("tactical_cash_cap_pct", 0.30))
                _tactical_cash = min(_raw_tc, _cap)
                compliance_denom = sizing_denom - _tactical_cash

        # ── Momentum lookup: rank (#1 = strongest), pct, alpha ───────────────
        scores_by_ticker: dict = {}
        n_ranked = 0
        if not df_scores.empty:
            ranked = df_scores.sort_values("Pct", ascending=False).reset_index(drop=True)
            n_ranked = len(ranked)
            for rank_i, row in enumerate(ranked.to_dict("records"), start=1):
                scores_by_ticker[row["Ticker"]] = {
                    "rank":  rank_i,
                    "pct":   float(row["Pct"]) if pd.notna(row["Pct"]) else 0.0,
                    "alpha": str(row.get("Alpha", "—")),
                    "blend": float(row["RawScore"]) if pd.notna(row.get("RawScore")) else 0.0,
                }

        # ── Breadth state — load persisted hysteresis-resolved floors ─────────
        _breadth_state: dict = {}
        _breadth_state_path = mws_analytics.BREADTH_STATE_JSON
        if os.path.exists(_breadth_state_path):
            try:
                with open(_breadth_state_path, "r", encoding="utf-8") as _f:
                    _breadth_state = json.load(_f)
            except Exception:
                pass  # fall back to current-day approximation if file missing/corrupt

        # ── Gate lookup ───────────────────────────────────────────────────────
        gates_by_ticker: dict = {}
        if df_gates is not None and not df_gates.empty:
            for row in df_gates.to_dict("records"):
                gates_by_ticker[str(row["ticker"])] = {
                    "action":  str(row.get("gate_action", "proceed")),
                    "z_score": row.get("z_score"),
                }

        # ── Sleeve layout ─────────────────────────────────────────────────────
        sleeves_l1 = policy["sleeves"]["level1"]
        sleeves_l2 = policy["sleeves"]["level2"]

        def _l2_mv(name: str) -> float:
            return float(hold.loc[hold["Class"] == name, "MV"].sum())

        def _l1_mv(name: str) -> float:
            return sum(_l2_mv(c) for c in sleeves_l1.get(name, {}).get("children", []))

        def _l1_sort(name: str) -> float:
            return -1.0 if name == "stabilizers" else _l1_mv(name)

        l1_sorted = sorted(sleeves_l1.keys(), key=_l1_sort, reverse=True)

        # ── Breadth-conditioned floor resolver ───────────────────────────────
        def _resolve_floor(l2_data: dict, sleeve_name: str = "") -> float:
            """Return the effective floor fraction for an L2 sleeve.

            For static floors (float): returns value directly.
            For breadth_conditioned floors (dict, v2.9.6):
              1. Prefers the hysteresis-resolved value from mws_breadth_state.json
                 (written by mws_analytics.compute_and_persist_breadth_states).
              2. Falls back to current-day raw breadth approximation if the state
                 file is absent or has no entry for this sleeve.
            """
            floor_val = l2_data.get("floor", 0.0)
            if not isinstance(floor_val, dict):
                return float(floor_val or 0.0)

            # Prefer persisted hysteresis-resolved floor
            if sleeve_name and sleeve_name in _breadth_state:
                stored = _breadth_state[sleeve_name]
                if "effective_floor" in stored:
                    return float(stored["effective_floor"])

            # Fallback: current-day approximation (no hysteresis)
            cond          = floor_val.get("breadth_condition", {})
            tickers       = l2_data.get("tickers", [])
            threshold     = int(cond.get("strong_breadth_threshold", 3))
            infeas_cond   = str(floor_val.get("infeasible_condition", ""))
            positive_count = sum(
                1 for t in tickers
                if scores_by_ticker.get(t, {}).get("blend", 0.0) > 0
            )
            floor_exit_count = sum(
                1 for t in tickers if t not in held_tickers
            )
            infeasible = (
                positive_count == 0
                or ("floor_exit_count >= 4" in infeas_cond and floor_exit_count >= 4)
            )
            if infeasible:
                return float(floor_val.get("infeasible_floor", 0.0))
            if positive_count >= threshold:
                return float(floor_val.get("strong_breadth_floor", 0.22))
            return float(floor_val.get("weak_breadth_floor", 0.12))

        # ── Action + basis per ticker ─────────────────────────────────────────
        def _action(ticker: str, cur_pct: float,
                    floor_pct: float, cap_pct: float) -> tuple:
            """Returns (label, bg_color, basis) where basis in
               'compliance_buy' | 'compliance_trim' | 'momentum_buy' |
               'momentum_trim' | 'hold' | 'spike_trim'."""
            gate_action = gates_by_ticker.get(ticker, {}).get("action", "proceed")
            pct         = scores_by_ticker.get(ticker, {}).get("pct", 0.5)

            if gate_action == "spike_trim":
                return "SPIKE-TRIM", "#b2ebf2", "spike_trim"

            if cur_pct < floor_pct - 0.1:
                base, basis = "BUY",  "compliance_buy"
            elif cur_pct > cap_pct + 0.1:
                base, basis = "TRIM", "compliance_trim"
            elif pct >= 0.65:
                # Absolute momentum filter (v2.9.7): block momentum buy if blend <= 0
                if scores_by_ticker.get(ticker, {}).get("blend", 0.0) <= 0:
                    return "HOLD", "#fff9c4", "hold|abs_filter"
                base, basis = "BUY",  "momentum_buy"
            elif pct <= 0.30:
                base, basis = "TRIM", "momentum_trim"
            else:
                return "HOLD", "#fff9c4", "hold"

            if base == "BUY" and gate_action == "defer":
                return "DEFER-BUY", "#ffe082", f"defer|{basis}"
            if base == "BUY":
                return "BUY",  "#c8e6c9", basis
            return "TRIM", "#ffcdd2", basis

        # ── Trade size estimation ─────────────────────────────────────────────
        def _est_trade(ticker: str, basis: str, l2_name: str,
                       l2_data: dict, denom: float,
                       buy_in_sleeve: list, trim_in_sleeve: list) -> tuple:
            """Returns (est_usd, est_shares, prefix) or (None, None, None).

            prefix is "~" for compliance-precise estimates, "≈" for
            momentum-proportional estimates (higher uncertainty).
            """
            core_basis = basis.replace("defer|", "")
            t_price = float(hold.loc[hold["Ticker"] == ticker, "Price"].iloc[0]) \
                      if not hold.loc[hold["Ticker"] == ticker].empty else 0.0

            if core_basis in ("compliance_buy", "compliance_trim"):
                floor_frac = _resolve_floor(l2_data, l2_name)
                cap_frac   = l2_data.get("cap")   or 0.0
                l2_total   = _l2_mv(l2_name)
                cur_frac   = l2_total / denom if denom > 0 else 0.0

                if core_basis == "compliance_buy":
                    deficit = (floor_frac - cur_frac) * denom
                    n = max(1, len(buy_in_sleeve))
                    est_usd = deficit / n
                else:  # compliance_trim
                    excess  = (cur_frac - cap_frac) * denom
                    t_mv    = float(hold.loc[hold["Ticker"] == ticker, "MV"].sum())
                    tot_mv  = sum(float(hold.loc[hold["Ticker"] == t, "MV"].sum())
                                  for t in trim_in_sleeve)
                    est_usd = excess * (t_mv / tot_mv) if tot_mv > 0 else excess / max(1, len(trim_in_sleeve))

                est_usd = max(0.0, est_usd)
                est_sh  = round(est_usd / t_price) if t_price > 0 else None
                return est_usd, est_sh, "~"

            if core_basis in ("momentum_buy", "momentum_trim"):
                # Momentum-proportional target weight within the L2 sleeve:
                # each ticker's fair share of l2_total is weighted by its
                # momentum percentile relative to all held tickers in the sleeve.
                held_in_l2 = [t for t in l2_data.get("tickers", [])
                               if t in held_tickers]
                total_pct = sum(scores_by_ticker.get(t, {}).get("pct", 0.0)
                                for t in held_in_l2)
                t_pct     = scores_by_ticker.get(ticker, {}).get("pct", 0.0)
                l2_total  = _l2_mv(l2_name)
                if total_pct > 0:
                    target_mv = (t_pct / total_pct) * l2_total
                else:
                    target_mv = l2_total / max(1, len(held_in_l2))
                t_mv    = float(hold.loc[hold["Ticker"] == ticker, "MV"].sum())
                est_usd = abs(target_mv - t_mv)
                est_usd = max(0.0, est_usd)
                est_sh  = round(est_usd / t_price) if t_price > 0 else None
                return est_usd, est_sh, "≈"

            return None, None, None

        # ── First pass: collect all ticker data ───────────────────────────────
        # ticker_data[ticker] = {l1, l2, l2_data, action, label, color, basis, cur_pct, floor_pct, cap_pct, denom}
        # cur_pct uses compliance_denom (floor/cap breach detection).
        # denom stored in ticker_data uses sizing_denom (trade $ sizing).
        ticker_data: dict = {}
        for l1_name in l1_sorted:
            l1_data    = sleeves_l1[l1_name]
            is_overlay = (l1_name == "stabilizers")
            sizing_d   = total_val if is_overlay else sizing_denom      # trade sizing
            comp_d     = total_val if is_overlay else compliance_denom  # floor/cap checks
            for l2_name in l1_data.get("children", []):
                l2_data   = sleeves_l2.get(l2_name, {})
                floor_pct = _resolve_floor(l2_data, l2_name) * 100
                cap_pct   = (l2_data.get("cap")   or 0) * 100
                l2_total  = _l2_mv(l2_name)
                cur_pct   = (l2_total / comp_d * 100) if comp_d > 0 else 0.0
                for ticker in l2_data.get("tickers", []):
                    if ticker not in held_tickers:
                        continue
                    label, color, basis = _action(ticker, cur_pct, floor_pct, cap_pct)
                    ticker_data[ticker] = dict(
                        l1=l1_name, l2=l2_name, l2_data=l2_data,
                        label=label, color=color, basis=basis,
                        cur_pct=cur_pct, floor_pct=floor_pct, cap_pct=cap_pct,
                        denom=sizing_d,  # sizing_denom for _est_trade dollar calculations
                    )

        # Per-sleeve BUY/TRIM ticker lists for trade-size splitting
        sleeve_buy:  dict = {}  # l2_name → [tickers]
        sleeve_trim: dict = {}
        for t, d in ticker_data.items():
            core = d["basis"].replace("defer|", "")
            if "buy" in core:
                sleeve_buy.setdefault(d["l2"], []).append(t)
            elif "trim" in core:
                sleeve_trim.setdefault(d["l2"], []).append(t)

        # ── TABLE 1: Portfolio Positions ──────────────────────────────────────
        t1_cols = [
            ("Sleeve / Ticker",
             "L1 and L2 sleeves with floor/cap policy limits; tickers indented within sleeve"),
            ("Rank",
             f"Momentum rank within universe (#1 = strongest momentum blend; {n_ranked} tickers ranked)"),
            ("Alpha vs VTI",
             "Total return outperformance vs VTI since portfolio inception date"),
            ("Weight",
             "Current weight as % of allocatable denominator (TPV minus overlays minus Bucket A); "
             "overlays weighted vs total portfolio value"),
            ("Market Value",
             "Current market value in USD"),
        ]
        t1_thead = (
            "<thead><tr>"
            + "".join(f'<th style="{_TH}" title="{desc}">{name}</th>'
                      for name, desc in t1_cols)
            + "</tr></thead>"
        )
        t1_rows: list[str] = []

        for l1_name in l1_sorted:
            l1_data    = sleeves_l1[l1_name]
            l1_cap     = l1_data.get("cap")
            children   = l1_data.get("children", [])
            is_overlay = (l1_name == "stabilizers")
            comp_d     = total_val if is_overlay else compliance_denom  # floor/cap display
            sizing_d   = total_val if is_overlay else sizing_denom       # weight display
            l1_total   = _l1_mv(l1_name)
            l1_cur_pct = (l1_total / comp_d * 100) if comp_d > 0 else 0.0
            l1_cap_str = f"{l1_cap * 100:.0f}%" if l1_cap else "overlay"

            t1_rows.append(
                f'<tr style="background:#d0dff0;">'
                f'<td colspan="5" style="border:1px solid #aac; padding:7px 10px; '
                f'font-weight:bold; font-size:12px;">'
                f'&#9654;&nbsp; {l1_name}'
                f'&ensp;|&ensp;cap: {l1_cap_str}'
                f'&ensp;|&ensp;current: {l1_cur_pct:.1f}%'
                f'&ensp;(${l1_total:,.0f})'
                f'</td></tr>'
            )

            for l2_name in sorted(children, key=_l2_mv, reverse=True):
                l2_data   = sleeves_l2.get(l2_name, {})
                floor_pct = _resolve_floor(l2_data, l2_name) * 100
                cap_pct   = (l2_data.get("cap")   or 0) * 100
                l2_total  = _l2_mv(l2_name)
                cur_pct   = (l2_total / comp_d * 100) if comp_d > 0 else 0.0  # compliance_denom

                if   cur_pct < floor_pct - 0.1: l2_status = "&#9888; BELOW FLOOR"
                elif cur_pct > cap_pct   + 0.1: l2_status = "&#9888; ABOVE CAP"
                else:                           l2_status = "&#10003;"

                t1_rows.append(
                    f'<tr style="background:#eef3f9;">'
                    f'<td colspan="5" style="border:1px solid #ccd; padding:5px 10px 5px 22px; '
                    f'font-style:italic; font-size:12px;">'
                    f'&nbsp;&nbsp;{l2_name}'
                    f'&ensp;floor {floor_pct:.0f}% &ndash; cap {cap_pct:.0f}%'
                    f'&ensp;|&ensp;current: <strong>{cur_pct:.1f}%</strong>'
                    f'&ensp;(${l2_total:,.0f})'
                    f'&ensp;{l2_status}'
                    f'</td></tr>'
                )

                held_in_sleeve = [
                    t for t in l2_data.get("tickers", []) if t in held_tickers
                ]
                held_in_sleeve.sort(
                    key=lambda t: scores_by_ticker.get(t, {}).get("pct", 0.0),
                    reverse=True,
                )
                for ticker in held_in_sleeve:
                    t_mv  = float(hold.loc[hold["Ticker"] == ticker, "MV"].sum())
                    t_wt  = (t_mv / sizing_d * 100) if sizing_d > 0 else 0.0
                    s     = scores_by_ticker.get(ticker, {})
                    rank  = s.get("rank", "—")
                    alpha = s.get("alpha", "—")
                    rank_str = f"#{rank} of {n_ranked}" if isinstance(rank, int) else "—"

                    t1_rows.append(
                        f'<tr>'
                        f'<td style="{_TD}">&nbsp;&nbsp;&nbsp;&nbsp;{ticker}</td>'
                        f'<td style="{_TDR}">{rank_str}</td>'
                        f'<td style="{_TDR}">{alpha}</td>'
                        f'<td style="{_TDR}">{t_wt:.1f}%</td>'
                        f'<td style="{_TDR}">${t_mv:,.0f}</td>'
                        f'</tr>'
                    )

        # Bucket A + Cash footer
        bucket_a_pct = (bucket_a_mv / total_val * 100) if total_val > 0 else 0.0
        t1_rows.append(
            f'<tr style="background:#f5f5f5;">'
            f'<td style="{_TD}"><em>Bucket A &mdash; protected liquidity (TREASURY_NOTE)</em></td>'
            f'<td style="{_TDC}">&#128274;</td>'
            f'<td style="{_TD}">—</td>'
            f'<td style="{_TDR}">{bucket_a_pct:.1f}%&nbsp;<small>(of TPV)</small></td>'
            f'<td style="{_TDR}">${bucket_a_mv:,.0f}</td>'
            f'</tr>'
        )
        cash_mv = float(hold.loc[hold["Class"] == "bucket_b", "MV"].sum())
        if cash_mv > 0:
            t1_rows.append(
                f'<tr>'
                f'<td style="{_TD}"><em>Cash (Bucket B)</em></td>'
                f'<td style="{_TD}">—</td><td style="{_TD}">—</td>'
                f'<td style="{_TDR}">{cash_mv / total_val * 100:.1f}%&nbsp;<small>(of TPV)</small></td>'
                f'<td style="{_TDR}">${cash_mv:,.0f}</td>'
                f'</tr>'
            )

        table1 = (
            f'<table style="border-collapse:collapse; width:100%; margin:8px 0 20px;">'
            f'{t1_thead}<tbody>{"".join(t1_rows)}</tbody></table>'
        )

        # ── TABLE 2: Recommended Actions ──────────────────────────────────────
        action_items = [
            (t, d) for t, d in ticker_data.items()
            if d["label"] != "HOLD"
        ]
        # Sort: compliance first, then by sleeve $MV desc, then rank asc
        def _action_sort(item):
            t, d = item
            compliance = 0 if "compliance" in d["basis"] else 1
            return (compliance, -_l2_mv(d["l2"]),
                    scores_by_ticker.get(t, {}).get("rank", 99))
        action_items.sort(key=_action_sort)

        # ── Budget waterfall (always computed, even with no action items) ──────
        # Pass 1: raw trade sizes
        raw_trades: dict = {}  # ticker → (raw_usd, pfx)
        for ticker, d in action_items:
            est_usd, _est_sh, est_pfx = _est_trade(
                ticker, d["basis"], d["l2"], d["l2_data"], d["denom"],
                sleeve_buy.get(d["l2"], []),
                sleeve_trim.get(d["l2"], []),
            )
            raw_trades[ticker] = (est_usd or 0.0, est_pfx or "~")

        # Pass 2: budget-constrained waterfall
        # Reserves (ring-fenced before any discretionary spending):
        #   1. DEFER-BUY reserve: gated buys will fire within 10 days → keep cash
        #   2. Policy cash reserve: policy["cash_reserve"] if enabled
        # Priority: compliance buys > deferred reserve > policy reserve >
        #           momentum buys > residual deployment (Phase 3)
        cr             = policy.get("cash_reserve", {})
        policy_reserve = (float(cr.get("target_cash_usd", 0))
                          if cr.get("enabled", False) else 0.0)
        deferred_reserve = sum(raw_trades[t][0] for t, d in action_items
                               if d["label"] == "DEFER-BUY")

        comp_sell_t = [t for t, d in action_items
                       if d["label"] in ("TRIM", "SPIKE-TRIM")
                       and d["basis"] in ("compliance_trim", "spike_trim")]
        mom_sell_t  = [t for t, d in action_items
                       if d["label"] in ("TRIM", "SPIKE-TRIM")
                       and "momentum" in d["basis"]]
        comp_buy_t  = [t for t, d in action_items
                       if d["label"] == "BUY" and "compliance_buy" in d["basis"]]
        mom_buy_t   = [t for t, d in action_items
                       if d["label"] == "BUY" and "momentum_buy" in d["basis"]]

        comp_sell_proceeds = sum(raw_trades[t][0] for t in comp_sell_t)
        mom_sell_proceeds  = sum(raw_trades[t][0] for t in mom_sell_t)
        comp_buy_need      = sum(raw_trades[t][0] for t in comp_buy_t)
        mom_buy_need       = sum(raw_trades[t][0] for t in mom_buy_t)
        total_available    = cash_mv + comp_sell_proceeds + mom_sell_proceeds

        # Phase 1: compliance buys — first claim on all available cash, capped at
        # per-event turnover limit (v2.9.9). Hard-limit compliance trades (Priority 1)
        # remain turnover-cap-exempt; regular L2 floor/cap compliance buys (Priority 3)
        # are subject to the 20% per-event cap. Any unfilled deficit is picked up
        # naturally in the next rebalance cycle when the compliance check re-fires.
        _exec_policy         = policy.get("governance", {}).get("execution", {})
        _max_turnover_pct    = float(_exec_policy.get("max_turnover", 0.20))
        _turnover_cap_usd    = total_val * _max_turnover_pct
        _cash_lim_scale      = (total_available   / comp_buy_need) if comp_buy_need > 0 else 1.0
        _turnover_lim_scale  = (_turnover_cap_usd / comp_buy_need) if comp_buy_need > 0 else 1.0
        comp_buy_scale       = min(1.0, _cash_lim_scale, _turnover_lim_scale)
        _comp_turnover_bound = (_turnover_lim_scale < _cash_lim_scale and comp_buy_scale < 0.999)
        cash_after_comp = total_available - comp_buy_need * comp_buy_scale

        # Ring-fence reserves from post-compliance cash
        deferred_reserve_actual = min(deferred_reserve, cash_after_comp)
        policy_reserve_actual   = min(policy_reserve,
                                      max(0.0, cash_after_comp - deferred_reserve_actual))
        cash_for_discretionary  = (cash_after_comp
                                   - deferred_reserve_actual
                                   - policy_reserve_actual)

        # Phase 2: momentum buys from discretionary pool
        mom_buy_scale  = min(1.0, cash_for_discretionary / mom_buy_need) if mom_buy_need > 0 else 1.0
        cash_after_mom = cash_for_discretionary - mom_buy_need * mom_buy_scale

        # Phase 3: deploy residual to highest-momentum HOLD tickers within sleeve cap headroom.
        # Greedy: highest-momentum HOLDs fill first up to their sleeve's cap headroom.
        deploy_items: list = []  # (ticker, d, deploy_usd, deploy_sh)
        residual = cash_after_mom
        if residual > 500:
            hold_candidates = sorted(
                [
                    (t, d) for t, d in ticker_data.items()
                    if d["label"] in ("HOLD", "hold|abs_filter")
                    # Absolute momentum filter (v2.9.7): residual only flows to positive-blend tickers
                    and scores_by_ticker.get(t, {}).get("blend", 0.0) > 0
                ],
                key=lambda x: scores_by_ticker.get(x[0], {}).get("pct", 0.0),
                reverse=True,
            )
            for ticker, d in hold_candidates:
                if residual < 100:
                    break
                cap_frac = d["l2_data"].get("cap") or 0.0
                l2_total = _l2_mv(d["l2"])
                cur_frac = l2_total / d["denom"] if d["denom"] > 0 else 0.0
                headroom = max(0.0, (cap_frac - cur_frac) * d["denom"])
                if headroom < 100:
                    continue
                deploy_usd = min(residual, headroom)
                t_price    = (float(hold.loc[hold["Ticker"] == ticker, "Price"].iloc[0])
                              if not hold.loc[hold["Ticker"] == ticker].empty else 0.0)
                deploy_sh  = round(deploy_usd / t_price) if t_price > 0 and deploy_usd > 0 else None
                deploy_items.append((ticker, d, deploy_usd, deploy_sh))
                residual -= deploy_usd

        cash_after_all = residual  # ≈ 0 when fully deployed; ≈ policy_reserve when reserve active

        # Scaled compliance/momentum trade sizes
        scaled_trades: dict = {}  # ticker → (scaled_usd, scaled_sh, pfx, note)
        for ticker, d in action_items:
            raw_usd, pfx = raw_trades[ticker]
            label   = d["label"]
            t_price = (float(hold.loc[hold["Ticker"] == ticker, "Price"].iloc[0])
                       if not hold.loc[hold["Ticker"] == ticker].empty else 0.0)
            if label == "DEFER-BUY":
                scaled_trades[ticker] = (None, None, pfx, "deferred")
            elif ticker in comp_buy_t:
                s_usd = raw_usd * comp_buy_scale
                s_sh  = round(s_usd / t_price) if t_price > 0 and s_usd > 0 else None
                if comp_buy_scale < 0.999:
                    note = (f"⚠ scaled {comp_buy_scale:.0%} — turnover cap (deficit queued)"
                            if _comp_turnover_bound else
                            f"⚠ scaled {comp_buy_scale:.0%} — cash limited")
                else:
                    note = ""
                scaled_trades[ticker] = (s_usd, s_sh, pfx, note)
            elif ticker in mom_buy_t:
                s_usd = raw_usd * mom_buy_scale
                s_sh  = round(s_usd / t_price) if t_price > 0 and s_usd > 0 else None
                note  = f"⚠ scaled {mom_buy_scale:.0%}" if mom_buy_scale < 0.999 else ""
                scaled_trades[ticker] = (s_usd, s_sh, pfx, note)
            else:  # sells — never scaled
                s_sh = round(raw_usd / t_price) if t_price > 0 and raw_usd > 0 else None
                scaled_trades[ticker] = (raw_usd, s_sh, pfx, "")

        if action_items or deploy_items:
            # ── Table 2 headers ────────────────────────────────────────────────
            t2_cols = [
                ("Ticker", ""),
                ("Sleeve", "L2 sleeve this ticker belongs to"),
                ("Rank", f"Momentum rank within universe, #1 = strongest (of {n_ranked} tickers)"),
                ("Vol z-score",
                 "2-day return divided by EWMA volatility (126-day span). "
                 "BUY deferred when z >= +2.0 (don't chase a spike). "
                 "SELL deferred when z <= -2.5 (don't sell into capitulation). "
                 "SPIKE-TRIM fires immediately when z >= +2.0 on a sell signal (sell into strength)."),
                ("Action",
                 "BUY / DEFER-BUY / TRIM / SPIKE-TRIM / DEPLOY — determined by sleeve compliance, "
                 "momentum rank, vol z-score, and residual cash deployment"),
                ("Basis",  "Primary reason the action was triggered"),
                ("Est. Trade",
                 "Budget-constrained trade size. Compliance buys have first claim; "
                 "DEFER-BUY reserve ring-fenced next; momentum buys from remainder; "
                 "DEPLOY = residual cash to highest-momentum in-band tickers within cap. "
                 "~ = compliance estimate; ≈ = momentum estimate."),
                ("Est. Shares", "Approximate number of shares at current price"),
            ]
            t2_thead = (
                "<thead><tr>"
                + "".join(f'<th style="{_TH}" title="{desc}">{name}</th>'
                          for name, desc in t2_cols)
                + "</tr></thead>"
            )

            # ── Pass 3: build rows ─────────────────────────────────────────────
            t2_rows: list[str] = []

            for ticker, d in action_items:
                s          = scores_by_ticker.get(ticker, {})
                rank       = s.get("rank", "—")
                rank_str   = f"#{rank} of {n_ranked}" if isinstance(rank, int) else "—"
                gate       = gates_by_ticker.get(ticker, {})
                z_val      = gate.get("z_score")
                z_str      = f"{z_val:+.2f}" if z_val is not None else "—"
                label      = d["label"]
                color      = d["color"]
                basis_raw  = d["basis"].replace("defer|", "")
                gate_pfx   = "Gate defer. " if "defer|" in d["basis"] else ""

                if basis_raw == "compliance_buy":
                    basis_str = (f"{gate_pfx}Sleeve {d['l2']}: "
                                 f"{d['cur_pct']:.1f}% below floor {d['floor_pct']:.0f}%")
                elif basis_raw == "compliance_trim":
                    basis_str = (f"{gate_pfx}Sleeve {d['l2']}: "
                                 f"{d['cur_pct']:.1f}% above cap {d['cap_pct']:.0f}%")
                elif basis_raw == "momentum_buy":
                    basis_str = f"{gate_pfx}Momentum rank {rank_str} (top of universe)"
                elif basis_raw == "momentum_trim":
                    basis_str = f"Momentum rank {rank_str} (bottom of universe)"
                elif basis_raw == "spike_trim":
                    basis_str = f"Sell into strength: gate z = {z_str}"
                else:
                    basis_str = basis_raw

                s_usd, s_sh, est_pfx, note = scaled_trades[ticker]
                if label == "DEFER-BUY":
                    est_usd_str = '<em style="color:#999;">deferred</em>'
                    est_sh_str  = "—"
                elif s_usd is not None and s_usd > 0:
                    est_usd_str = f"{est_pfx}${s_usd:,.0f}"
                    if note:
                        est_usd_str += f' <small style="color:#e65100;">{note}</small>'
                    est_sh_str = f"{est_pfx}{s_sh:,} sh" if s_sh and s_sh > 0 else "—"
                else:
                    est_usd_str = "—"
                    est_sh_str  = "—"

                t2_rows.append(
                    f'<tr>'
                    f'<td style="{_TD}"><strong>{ticker}</strong></td>'
                    f'<td style="{_TD}">{d["l2"]}</td>'
                    f'<td style="{_TDR}">{rank_str}</td>'
                    f'<td style="{_TDR}">{z_str}</td>'
                    f'<td style="border:1px solid #ddd; padding:5px 8px; text-align:center; '
                    f'font-weight:bold; font-size:12px; background:{color};">{label}</td>'
                    f'<td style="{_TD}">{basis_str}</td>'
                    f'<td style="{_TDR}">{est_usd_str}</td>'
                    f'<td style="{_TDR}">{est_sh_str}</td>'
                    f'</tr>'
                )

            # Phase 3 deploy rows (residual cash → highest-momentum HOLDs)
            for ticker, d, deploy_usd, deploy_sh in deploy_items:
                s        = scores_by_ticker.get(ticker, {})
                rank     = s.get("rank", "—")
                rank_str = f"#{rank} of {n_ranked}" if isinstance(rank, int) else "—"
                gate     = gates_by_ticker.get(ticker, {})
                z_val    = gate.get("z_score")
                z_str    = f"{z_val:+.2f}" if z_val is not None else "—"
                usd_str  = f"≈${deploy_usd:,.0f}"
                sh_str   = f"≈{deploy_sh:,} sh" if deploy_sh and deploy_sh > 0 else "—"
                t2_rows.append(
                    f'<tr>'
                    f'<td style="{_TD}"><strong>{ticker}</strong></td>'
                    f'<td style="{_TD}">{d["l2"]}</td>'
                    f'<td style="{_TDR}">{rank_str}</td>'
                    f'<td style="{_TDR}">{z_str}</td>'
                    f'<td style="border:1px solid #ddd; padding:5px 8px; text-align:center; '
                    f'font-weight:bold; font-size:12px; background:#e3f2fd;">DEPLOY</td>'
                    f'<td style="{_TD}">Residual cash deployment — rank {rank_str} in-band</td>'
                    f'<td style="{_TDR}">{usd_str}</td>'
                    f'<td style="{_TDR}">{sh_str}</td>'
                    f'</tr>'
                )

            # ── Budget summary ─────────────────────────────────────────────────
            deploy_total = sum(u for _, _, u, _ in deploy_items)

            _BS  = "border:1px solid #ddd; padding:4px 10px; font-size:12px; white-space:nowrap;"
            _BSR = ("border:1px solid #ddd; padding:4px 10px; text-align:right; "
                    "font-size:12px; font-family:monospace; white-space:nowrap;")
            _BSH = ("background:#f0f4f8; border:1px solid #bcd; padding:5px 10px; "
                    "font-size:12px; font-weight:bold;")
            _SEP = f'<tr><td colspan="2" style="border:0; padding:2px 0;"></td></tr>'

            def _brow(lbl, val, bold=False, warn=False):
                ls = _BS  + (" font-weight:bold;" if bold else "")
                vs = _BSR + (" font-weight:bold;" if bold else "") + (" color:#e65100;" if warn else "")
                return f'<tr><td style="{ls}">{lbl}</td><td style="{vs}">${val:,.0f}</td></tr>'

            budget_html = (
                f'<table style="border-collapse:collapse; margin:4px 0 20px;">'
                f'<tbody>'
                f'<tr><td colspan="2" style="{_BSH}">Trade Budget</td></tr>'
                + _brow("Cash on hand", cash_mv)
                + _brow("+ Sell proceeds (trims / spike-trims)",
                        comp_sell_proceeds + mom_sell_proceeds)
                + _brow("= Total available", total_available, bold=True)
                + _SEP
            )
            if deferred_reserve > 0:
                budget_html += _brow(
                    "− DEFER-BUY reserve (ring-fenced)", deferred_reserve_actual,
                    warn=(deferred_reserve_actual < deferred_reserve))
            if policy_reserve > 0:
                budget_html += _brow("− Policy cash reserve", policy_reserve_actual)
            budget_html += (
                _brow("= Deployable today",
                      total_available - deferred_reserve_actual - policy_reserve_actual,
                      bold=True)
                + _SEP
                + _brow("Compliance buys"
                        + (" &nbsp;⚠ scaled" if comp_buy_scale < 0.999 else ""),
                        comp_buy_need * comp_buy_scale, warn=(comp_buy_scale < 0.999))
                + _brow("Momentum buys"
                        + (" &nbsp;⚠ scaled" if mom_buy_scale < 0.999 else ""),
                        mom_buy_need * mom_buy_scale, warn=(mom_buy_scale < 0.999))
            )
            if deploy_total > 0:
                budget_html += _brow("Residual deployment (DEPLOY)", deploy_total)
            budget_html += (
                _brow("= Net cash after trades", cash_after_all, bold=True,
                      warn=(cash_after_all < 0))
            )
            if deferred_reserve > 0:
                budget_html += (
                    _SEP
                    + _brow("DEFER-BUY — fires within 10 days (reserved)", deferred_reserve)
                )
            budget_html += '</tbody></table>'

            table2 = (
                f'<table style="border-collapse:collapse; width:100%; margin:8px 0 8px;">'
                f'{t2_thead}<tbody>{"".join(t2_rows)}</tbody></table>'
            )
            t2_section = '<h3>Recommended Actions</h3>' + table2 + budget_html
        else:
            t2_section = '<p><em>No actions required — all sleeves in band, no momentum signals.</em></p>'

        # ── Status header ─────────────────────────────────────────────────────
        dd_state = dd.get("state", "normal").upper()
        dd_pct   = abs(dd.get("drawdown", 0)) * 100
        peak_tpv = dd.get("peak_tpv", total_val)
        status_html = (
            f'<p style="font-size:13px; margin:4px 0 12px;">'
            f'<strong>TPV:</strong> ${total_val:,.0f} (as of {val_asof})'
            f'&ensp;|&ensp;<strong>Drawdown:</strong> {dd_state} &mdash; {dd_pct:.1f}% from peak'
            f' (${peak_tpv:,.0f})<br>'
            f'<strong>Sizing denominator:</strong> ${sizing_denom:,.0f}'
            f' (TPV &minus; overlays &minus; Bucket A)'
            + (f'&ensp;|&ensp;<strong>Compliance denominator:</strong> ${compliance_denom:,.0f}'
               f' (excl. ${_tactical_cash:,.0f} tactical cash &mdash; abs-filter blocked)'
               if _tactical_cash > 0 else '')
            + f'</p>'
        )

        # ── Colour legend ─────────────────────────────────────────────────────
        legend = (
            '<p style="font-size:11px; color:#666; margin:12px 0 0;">'
            '<strong>Action colours:</strong>&ensp;'
            '<span style="background:#c8e6c9; padding:1px 6px; border-radius:3px;">BUY</span>&ensp;'
            '<span style="background:#ffe082; padding:1px 6px; border-radius:3px;">DEFER-BUY</span>&ensp;'
            '<span style="background:#fff9c4; padding:1px 6px; border-radius:3px;">HOLD</span>&ensp;'
            '<span style="background:#ffcdd2; padding:1px 6px; border-radius:3px;">TRIM</span>&ensp;'
            '<span style="background:#b2ebf2; padding:1px 6px; border-radius:3px;">SPIKE-TRIM</span>'
            '&ensp;|&ensp;Hover column headers for definitions.'
            '</p>'
        )

        # ── Write mws_precomputed_targets.json for interactive LLM runs ─────────
        # This is the token-lean alternative to embedding the full analytics bundle in
        # the prompt.  It is auto-generated every run — never edit by hand.
        # SYNC GUARANTEE: generated here from the same variables that drive the HTML
        # tables, so it is always consistent with the email output.
        try:
            _regime = dd.get("state", "normal")
            _dd_pct = round(abs(dd.get("drawdown", 0)) * 100, 2)

            # Per-ticker records
            _tickers_out: dict = {}

            # Action tickers (BUY / TRIM / DEFER-BUY / SPIKE-TRIM)
            for _t, _d in action_items:
                _s   = scores_by_ticker.get(_t, {})
                _g   = gates_by_ticker.get(_t, {})
                _mv  = float(hold.loc[hold["Ticker"] == _t, "MV"].sum())
                _px  = float(hold.loc[hold["Ticker"] == _t, "Price"].iloc[0]) \
                       if not hold.loc[hold["Ticker"] == _t].empty else 0.0
                _sh  = float(hold.loc[hold["Ticker"] == _t, "Shares"].iloc[0]) \
                       if not hold.loc[hold["Ticker"] == _t].empty else 0.0
                _su, _ssh, _spfx, _snote = scaled_trades.get(_t, (None, None, "~", ""))
                _tickers_out[_t] = {
                    "shares":        round(_sh, 5),
                    "price":         round(_px, 4),
                    "mv":            round(_mv, 2),
                    "sleeve_l1":     _d["l1"],
                    "sleeve_l2":     _d["l2"],
                    "current_pct":   round(_d["cur_pct"], 2),
                    "floor_pct":     round(_d["floor_pct"], 1),
                    "cap_pct":       round(_d["cap_pct"], 1),
                    "momentum_rank": _s.get("rank", None),
                    "momentum_pct":  round(_s.get("pct", 0) * 100, 1),
                    "raw_score":     round(_s.get("blend", 0), 4),
                    "gate_z":        round(_g.get("z_score", 0), 2) if _g.get("z_score") is not None else None,
                    "gate_action":   _g.get("action", "proceed"),
                    "action":        _d["label"],
                    "basis":         _d["basis"],
                    "est_usd":       round(_su, 0) if _su is not None else None,
                    "est_shares":    _ssh,
                    "scale_note":    _snote,
                }

            # DEPLOY tickers (residual cash deployment)
            for _t, _d, _du, _ds in deploy_items:
                _s   = scores_by_ticker.get(_t, {})
                _g   = gates_by_ticker.get(_t, {})
                _mv  = float(hold.loc[hold["Ticker"] == _t, "MV"].sum())
                _px  = float(hold.loc[hold["Ticker"] == _t, "Price"].iloc[0]) \
                       if not hold.loc[hold["Ticker"] == _t].empty else 0.0
                _sh  = float(hold.loc[hold["Ticker"] == _t, "Shares"].iloc[0]) \
                       if not hold.loc[hold["Ticker"] == _t].empty else 0.0
                _tickers_out[_t] = {
                    "shares":        round(_sh, 5),
                    "price":         round(_px, 4),
                    "mv":            round(_mv, 2),
                    "sleeve_l1":     _d["l1"],
                    "sleeve_l2":     _d["l2"],
                    "current_pct":   round(_d["cur_pct"], 2),
                    "floor_pct":     round(_d["floor_pct"], 1),
                    "cap_pct":       round(_d["cap_pct"], 1),
                    "momentum_rank": _s.get("rank", None),
                    "momentum_pct":  round(_s.get("pct", 0) * 100, 1),
                    "raw_score":     round(_s.get("blend", 0), 4),
                    "gate_z":        round(_g.get("z_score", 0), 2) if _g.get("z_score") is not None else None,
                    "gate_action":   _g.get("action", "proceed"),
                    "action":        "DEPLOY",
                    "basis":         "residual_deployment",
                    "est_usd":       round(_du, 0),
                    "est_shares":    _ds,
                    "scale_note":    "",
                }

            # HOLD tickers (not in action_items or deploy_items)
            _active_set = set(t for t, _ in action_items) | set(t for t, _, _, _ in deploy_items)
            for _t, _d in ticker_data.items():
                if _t in _active_set:
                    continue
                _s   = scores_by_ticker.get(_t, {})
                _g   = gates_by_ticker.get(_t, {})
                _mv  = float(hold.loc[hold["Ticker"] == _t, "MV"].sum())
                _px  = float(hold.loc[hold["Ticker"] == _t, "Price"].iloc[0]) \
                       if not hold.loc[hold["Ticker"] == _t].empty else 0.0
                _sh  = float(hold.loc[hold["Ticker"] == _t, "Shares"].iloc[0]) \
                       if not hold.loc[hold["Ticker"] == _t].empty else 0.0
                _tickers_out[_t] = {
                    "shares":        round(_sh, 5),
                    "price":         round(_px, 4),
                    "mv":            round(_mv, 2),
                    "sleeve_l1":     _d["l1"],
                    "sleeve_l2":     _d["l2"],
                    "current_pct":   round(_d["cur_pct"], 2),
                    "floor_pct":     round(_d["floor_pct"], 1),
                    "cap_pct":       round(_d["cap_pct"], 1),
                    "momentum_rank": _s.get("rank", None),
                    "momentum_pct":  round(_s.get("pct", 0) * 100, 1),
                    "raw_score":     round(_s.get("blend", 0), 4),
                    "gate_z":        round(_g.get("z_score", 0), 2) if _g.get("z_score") is not None else None,
                    "gate_action":   _g.get("action", "proceed"),
                    "action":        "HOLD",
                    "basis":         _d["basis"],
                    "est_usd":       None,
                    "est_shares":    None,
                    "scale_note":    "",
                }

            # Sleeve summary
            _sleeves_out: dict = {}
            for _l2n in set(
                d["l2"] for d in ticker_data.values()
                if not sleeves_l2.get(d["l2"], {}).get("exclude_from_denominator", False)
            ):
                _l2d  = sleeves_l2.get(_l2n, {})
                _l2mv = _l2_mv(_l2n)
                _fl   = _resolve_floor(_l2d, _l2n)
                _cap  = _l2d.get("cap") or 0.0
                _cpt  = (_l2mv / compliance_denom * 100) if compliance_denom > 0 else 0.0
                if   _cpt < _fl * 100 - 0.1:  _st = "BELOW_FLOOR"
                elif _cpt > _cap * 100 + 0.1:  _st = "ABOVE_CAP"
                else:                           _st = "in_band"
                _sleeves_out[_l2n] = {
                    "mv":          round(_l2mv, 2),
                    "current_pct": round(_cpt, 2),
                    "floor_pct":   round(_fl * 100, 1),
                    "cap_pct":     round(_cap * 100, 1),
                    "status":      _st,
                }

            # Overlay summary
            _ov_out: dict = {}
            for _ot in ["DBMF", "KMLM"]:
                _omv = float(hold.loc[hold["Ticker"] == _ot, "MV"].sum()) if _ot in held_tickers else 0.0
                _ov_out[_ot] = {"mv": round(_omv, 2), "pct_tpv": round(_omv / total_val * 100, 2)}

            # Compute a content hash of the holdings CSV so the fast-exit check
            # can detect holdings changes without relying on file mtime (which is
            # reset to checkout time by git operations, making mtime unreliable).
            import hashlib as _hl
            try:
                with open(mws_analytics.HOLDINGS_CSV, "rb") as _hf:
                    _holdings_hash = _hl.md5(_hf.read()).hexdigest()
            except Exception:
                _holdings_hash = ""

            _targets_doc = {
                "_runtime_meta": {
                    "generated":      TODAY,
                    "source_scripts": ["mws_analytics.py", "mws_runner.py"],
                    "note": (
                        "Auto-generated — do not edit. Overwritten on every run. "
                        "Use this file to drive interactive LLM runs instead of "
                        "re-deriving targets from raw analytics."
                    ),
                },
                "run_date":              TODAY,
                "holdings_hash":         _holdings_hash,
                "tpv":                   round(total_val, 2),
                "sizing_denom":          round(sizing_denom, 2),
                "compliance_denom":      round(compliance_denom, 2),
                "tactical_cash_active":  _tactical_cash > 0,
                "tactical_cash_usd":     round(_tactical_cash, 2),
                "drawdown_pct":          _dd_pct,
                "regime":                _regime,
                "breadth_state":         _breadth_state,
                "portfolio":             _tickers_out,
                "sleeves":               _sleeves_out,
                "overlays": {
                    "total_pct_tpv": round(overlay_mv / total_val * 100, 2),
                    "band":          [0.06, 0.12],
                    **_ov_out,
                },
                "bucket_a": {
                    "mv":           round(bucket_a_mv, 2),
                    "min_required": 45000,
                    "status":       "ok" if bucket_a_mv >= 45000 else "BELOW_MIN",
                },
                "trade_budget": {
                    "cash_on_hand":            round(cash_mv, 2),
                    "comp_sell_proceeds":      round(comp_sell_proceeds, 2),
                    "mom_sell_proceeds":       round(mom_sell_proceeds, 2),
                    "total_available":         round(total_available, 2),
                    "comp_buy_need":           round(comp_buy_need, 2),
                    "comp_buy_scale":          round(comp_buy_scale, 4),
                    "comp_turnover_bound":     _comp_turnover_bound,
                    "mom_buy_need":            round(mom_buy_need, 2),
                    "mom_buy_scale":           round(mom_buy_scale, 4),
                    "deploy_total":            round(sum(u for _, _, u, _ in deploy_items), 2),
                    "cash_after_all":          round(cash_after_all, 2),
                    "turnover_cap_pct":        round(_max_turnover_pct * 100, 1),
                    "turnover_cap_usd":        round(_turnover_cap_usd, 2),
                    "estimated_signal_sells":  round(mom_sell_proceeds, 2),
                    "estimated_signal_buys":   round(mom_buy_need * mom_buy_scale + sum(u for _, _, u, _ in deploy_items), 2),
                },
            }

            with open(PRECOMPUTED_TARGETS_FILE, "w", encoding="utf-8") as _f:
                json.dump(_targets_doc, _f, indent=2, ensure_ascii=False)
            log.info("Precomputed targets written → %s", PRECOMPUTED_TARGETS_FILE)

        except Exception as _json_err:
            log.warning("Could not write precomputed targets: %s", _json_err, exc_info=True)

        return (
            status_html
            + '<h3>Portfolio Positions</h3>' + table1
            + t2_section
            + legend
        )

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


def send_compliance_email(analytics: dict) -> None:
    """Send a compliance/status digest email when SKIP_LLM=true.

    Contains: portfolio snapshot, sleeve compliance status, momentum rankings,
    and Python-computed trade candidates (labelled as pending LLM news review).
    Does NOT contain trade proposals — those require live news overlay via a
    Claude Code session with WebSearch access.
    """
    password  = os.environ.get("GMAIL_APP_PASSWORD")
    from_addr = os.environ.get("GMAIL_FROM", "bhatnagar.vivek@gmail.com")
    to_addr   = os.environ.get("GMAIL_TO",   "bhatnagar.vivek@gmail.com")

    if not password:
        log.warning("GMAIL_APP_PASSWORD not set — skipping compliance email.")
        return

    total_val = analytics["total_val"]
    dd        = abs(analytics["drawdown"].get("drawdown", 0))  # drawdown dict → float

    if dd >= 0.30:
        regime_label = "🔴 HARD LIMIT"
    elif dd >= 0.22:
        regime_label = "⚠️ SOFT LIMIT"
    else:
        regime_label = "🟢 NORMAL"

    subject = f"MWS Digest | {TODAY} | TPV ${total_val:,.0f} | {regime_label}"

    # ── Load precomputed targets for sleeve / breadth / trade data ────────────
    targets: dict = {}
    try:
        with open(PRECOMPUTED_TARGETS_FILE, "r", encoding="utf-8") as _f:
            targets = json.load(_f)
    except Exception as _e:
        log.warning("Could not read precomputed targets for compliance email: %s", _e)

    sleeves      = targets.get("sleeves", {})
    overlays     = targets.get("overlays", {})
    bucket_a     = targets.get("bucket_a", {})
    breadth_ai   = targets.get("breadth_state", {}).get("ai_tech", {})
    portfolio    = targets.get("portfolio", {})
    trade_budget = targets.get("trade_budget", {})
    tact_cash    = targets.get("tactical_cash_active", False)
    sizing_denom = targets.get("sizing_denom", 0.0)

    _TH  = "background:#f0f4f8; border:1px solid #bcd; padding:6px 10px; text-align:left; white-space:nowrap; font-size:12px;"
    _THR = "background:#f0f4f8; border:1px solid #bcd; padding:6px 10px; text-align:right; white-space:nowrap; font-size:12px;"
    _TD  = "border:1px solid #ddd; padding:5px 8px; white-space:nowrap; font-size:12px;"
    _TDR = "border:1px solid #ddd; padding:5px 8px; text-align:right; white-space:nowrap; font-size:12px;"
    _TDC = "border:1px solid #ddd; padding:5px 8px; text-align:center; white-space:nowrap; font-size:12px;"

    # ── Header: portfolio snapshot ─────────────────────────────────────────────
    ba_ok   = "✅" if bucket_a.get("status") == "ok" else "⚠️"
    tc_flag = "⚠️ ACTIVE" if tact_cash else "—"
    ai_floor = f"{breadth_ai.get('effective_floor', 0)*100:.0f}% ({breadth_ai.get('current_category', '?')})"

    header_html = f"""
<h1>MWS Compliance Digest — {TODAY}</h1>
<table style="border-collapse:collapse; margin-bottom:16px;">
  <tr><td style="{_TD}"><strong>TPV</strong></td><td style="{_TDR}">${total_val:,.0f}</td>
      <td style="{_TD} padding-left:24px;"><strong>sizing_denom</strong></td><td style="{_TDR}">${sizing_denom:,.0f}</td></tr>
  <tr><td style="{_TD}"><strong>Drawdown</strong></td><td style="{_TDR}">{dd*100:.1f}%</td>
      <td style="{_TD} padding-left:24px;"><strong>Regime</strong></td><td style="{_TDR}">{regime_label}</td></tr>
  <tr><td style="{_TD}"><strong>Bucket A</strong></td><td style="{_TDR}">${bucket_a.get('mv', 0):,.0f} {ba_ok}</td>
      <td style="{_TD} padding-left:24px;"><strong>Tactical Cash</strong></td><td style="{_TDR}">{tc_flag}</td></tr>
  <tr><td style="{_TD}"><strong>ai_tech floor</strong></td><td style="{_TDR}">{ai_floor}</td>
      <td style="{_TD} padding-left:24px;"><strong>Cash</strong></td><td style="{_TDR}">${trade_budget.get('cash_on_hand', 0):,.2f}</td></tr>
</table>
"""

    # ── Sleeve compliance table ────────────────────────────────────────────────
    any_breach = any(s.get("status") != "in_band" for s in sleeves.values())
    sleeve_header = "⚠️ SLEEVE COMPLIANCE — BREACH DETECTED" if any_breach else "✅ SLEEVE COMPLIANCE — ALL IN BAND"
    sleeve_rows = ""
    for sl_name, sl_data in sleeves.items():
        status = sl_data.get("status", "?")
        icon   = "✅" if status == "in_band" else "⚠️"
        row_bg = " background:#fff8e1;" if status != "in_band" else ""
        sleeve_rows += (
            f'<tr><td style="{_TD}{row_bg}">{sl_name}</td>'
            f'<td style="{_TDR}{row_bg}">${sl_data["mv"]:,.0f}</td>'
            f'<td style="{_TDR}{row_bg}">{sl_data["current_pct"]:.1f}%</td>'
            f'<td style="{_TDR}{row_bg}">{sl_data["floor_pct"]:.0f}% – {sl_data["cap_pct"]:.0f}%</td>'
            f'<td style="{_TDC}{row_bg}">{icon} {status}</td></tr>'
        )
    # Overlays row
    ov_pct  = overlays.get("total_pct_tpv", 0.0)
    ov_band = overlays.get("band", [0.06, 0.12])
    ov_mv   = sum(v.get("mv", 0) for k, v in overlays.items() if isinstance(v, dict) and "mv" in v)
    ov_ok   = ov_band[0]*100 <= ov_pct <= ov_band[1]*100
    ov_icon = "✅" if ov_ok else "⚠️"
    sleeve_rows += (
        f'<tr><td style="{_TD}">managed_futures (overlay)</td>'
        f'<td style="{_TDR}">${ov_mv:,.0f}</td>'
        f'<td style="{_TDR}">{ov_pct:.1f}% TPV</td>'
        f'<td style="{_TDR}">{ov_band[0]*100:.0f}% – {ov_band[1]*100:.0f}%</td>'
        f'<td style="{_TDC}">{ov_icon} {"in_band" if ov_ok else "OUT_OF_BAND"}</td></tr>'
    )

    sleeve_html = f"""
<h2>{sleeve_header}</h2>
<table style="border-collapse:collapse; width:100%;">
  <tr><th style="{_TH}">Sleeve</th><th style="{_THR}">MV</th><th style="{_THR}">Current %</th>
      <th style="{_THR}">Floor–Cap</th><th style="{_THR}">Status</th></tr>
{sleeve_rows}
</table>
"""

    # ── Trade candidates (Python-computed; no news overlay) ────────────────────
    trade_rows = ""
    has_trades = False
    for ticker, td in portfolio.items():
        action = td.get("action", "HOLD")
        if action == "HOLD":
            continue
        has_trades = True
        est_usd    = td.get("est_usd") or 0.0
        est_shares = td.get("est_shares")
        shares_str = str(est_shares) if est_shares is not None else "—"
        basis      = td.get("basis", "")
        gate_z     = td.get("gate_z", 0.0)
        gate_act   = td.get("gate_action", "proceed")
        gate_str   = f"{gate_z:+.2f} ({gate_act})"
        action_color = "#d32f2f" if "TRIM" in action else "#1b5e20"
        trade_rows += (
            f'<tr><td style="{_TD}"><strong style="color:{action_color}">{action}</strong></td>'
            f'<td style="{_TD}">{ticker}</td>'
            f'<td style="{_TD}">{td.get("sleeve_l2","")}</td>'
            f'<td style="{_TDR}">{td.get("momentum_pct",0):.0f}%</td>'
            f'<td style="{_TDR}">{td.get("raw_score",0):.3f}</td>'
            f'<td style="{_TD}">{basis}</td>'
            f'<td style="{_TDR}">${est_usd:,.0f}</td>'
            f'<td style="{_TDR}">{shares_str}</td>'
            f'<td style="{_TDC}">{gate_str}</td></tr>'
        )

    if has_trades:
        comp_proceeds = trade_budget.get("comp_sell_proceeds", 0.0)
        mom_proceeds  = trade_budget.get("mom_sell_proceeds", 0.0)
        total_avail   = trade_budget.get("total_available", 0.0)
        scale_pct     = trade_budget.get("mom_buy_scale", 1.0) * 100
        trade_section = f"""
<h2>⚙️ PYTHON-COMPUTED TRADE CANDIDATES — pending live news review</h2>
<p style="color:#555; font-size:12px; font-style:italic;">
  These are deterministic Python outputs. Trade proposals (go/no-go + news overlay)
  are produced by the 10 AM Claude Code session with live WebSearch.
</p>
<p style="font-size:12px;">
  <strong>Budget:</strong> compliance sells ${comp_proceeds:,.0f} + momentum sells ${mom_proceeds:,.0f}
  = ${total_avail:,.0f} available | momentum buy scale {scale_pct:.0f}%
</p>
<table style="border-collapse:collapse; width:100%;">
  <tr><th style="{_TH}">Action</th><th style="{_TH}">Ticker</th><th style="{_TH}">Sleeve</th>
      <th style="{_THR}">Momentum%</th><th style="{_THR}">Score</th><th style="{_TH}">Basis</th>
      <th style="{_THR}">Est.$</th><th style="{_THR}">Shares</th><th style="{_THR}">Gate z</th></tr>
{trade_rows}
</table>
"""
    else:
        trade_section = "<h2>✅ NO TRADES INDICATED</h2><p>All sleeves in band, no momentum signals above threshold.</p>"

    # ── Footer ─────────────────────────────────────────────────────────────────
    footer_html = """
<hr>
<p style="color:#888; font-size:11px;">
  <strong>Note:</strong> This digest contains Python-computed compliance status only.
  Trade proposals with live news overlay are produced by the scheduled Claude Code session (10 AM ET).
  To trigger an on-demand full LLM run: GitHub Actions → MWS Portfolio Run → Run workflow → set skip_llm=false.
</p>
"""

    # ── Assemble + send ────────────────────────────────────────────────────────
    body_content = header_html + sleeve_html + trade_section
    body_content += '<hr><h2>Portfolio State</h2>' + _build_portfolio_tables(analytics)
    body_content += footer_html

    chart_path = mws_analytics.CHART_FILENAME
    has_chart  = os.path.exists(chart_path)
    chart_cid  = "mws_equity_curve"

    if has_chart:
        chart_tag = (
            f'<hr style="margin-top:24px">'
            f'<img src="cid:{chart_cid}" style="max-width:100%;height:auto;" '
            f'alt="MWS Equity Curve">'
        )
        body_content += chart_tag

    html_body = f"<html><head>{_EMAIL_CSS}</head><body>{body_content}</body></html>"

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    alt = MIMEMultipart("alternative")
    plain_text = (
        f"MWS Compliance Digest — {TODAY}\n"
        f"TPV: ${total_val:,.0f} | Drawdown: {dd*100:.1f}% | {regime_label}\n\n"
        f"Sleeve breaches: {sum(1 for s in sleeves.values() if s.get('status') != 'in_band')}\n"
        f"Trade candidates: {sum(1 for td in portfolio.values() if td.get('action') != 'HOLD')}\n\n"
        f"Full trade proposals via 10 AM Claude Code session."
    )
    alt.attach(MIMEText(plain_text, "plain", "utf-8"))

    if has_chart:
        related = MIMEMultipart("related")
        related.attach(MIMEText(html_body, "html", "utf-8"))
        with open(chart_path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", f"<{chart_cid}>")
        img.add_header("Content-Disposition", "inline", filename=chart_path)
        related.attach(img)
        alt.attach(related)
    else:
        alt.attach(MIMEText(html_body, "html", "utf-8"))

    msg.attach(alt)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, password)
        smtp.sendmail(from_addr, to_addr, msg.as_string())

    log.info("Compliance digest email sent to %s", to_addr)


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

def _print_benchmark_report(t_total: float) -> None:
    """Print a consolidated timing + token benchmark table at end of run."""
    pt = mws_analytics._PHASE_TIMINGS  # analytics sub-phases
    rt = _RUN_TIMINGS                   # runner-level phases
    tok = _TOKEN_USAGE
    prompt_chars = rt.get("prompt_chars", 0)

    log.info("")
    log.info("╔═══════════════════════════════════════════════════════╗")
    log.info("║          ⏱  MWS BENCHMARK REPORT                     ║")
    log.info("╠═══════════════════════════════════════════════════════╣")
    log.info("║ ANALYTICS PHASE                                       ║")
    log.info("║  load_system_files      %6.2fs                       ║", pt.get("load_system_files", 0))
    log.info("║  run_mws_audit          %6.2fs                       ║", pt.get("run_mws_audit", 0))
    log.info("║  calculate_portfolio_value %6.2fs                    ║", pt.get("calculate_portfolio_value", 0))
    log.info("║  update_performance_log %6.2fs                       ║", pt.get("update_performance_log", 0))
    log.info("║  check_drawdown_state×2 %6.2fs                       ║", pt.get("check_drawdown_state", 0))
    log.info("║  generate_rankings      %6.2fs                       ║", pt.get("generate_rankings", 0))
    log.info("║  execution_gates (all)  %6.2fs                       ║", pt.get("execution_gates", 0))
    log.info("║  generate_policy_runtime%6.2fs                       ║", pt.get("generate_policy_runtime", 0))
    log.info("║  ─────────────────────────────                        ║")
    log.info("║  run_analytics() total  %6.2fs                       ║", rt.get("analytics", 0))
    log.info("╠═══════════════════════════════════════════════════════╣")
    log.info("║ RUNNER PHASE                                          ║")
    log.info("║  chart generation       %6.2fs                       ║", rt.get("chart", 0))
    log.info("║  portfolio_tables       %6.2fs                       ║", rt.get("portfolio_tables", 0))
    if "build_prompt" in rt:
        log.info("║  build_prompt           %6.2fs                       ║", rt.get("build_prompt", 0))
        log.info("║  prompt size            %6d chars (~%dk tokens)    ║",
                 prompt_chars, prompt_chars // 4000)
    if "llm_call" in rt:
        log.info("║  LLM call (wall clock)  %6.2fs                       ║", rt.get("llm_call", 0))
    log.info("╠═══════════════════════════════════════════════════════╣")
    if tok:
        log.info("║ TOKEN USAGE                                           ║")
        log.info("║  input tokens           %8d                       ║", tok.get("input_tokens", 0))
        log.info("║  output tokens          %8d                       ║", tok.get("output_tokens", 0))
        log.info("║  cache read tokens      %8d                       ║", tok.get("cache_read_tokens", 0))
        total_tok = tok.get("input_tokens", 0) + tok.get("output_tokens", 0)
        log.info("║  total tokens           %8d                       ║", total_tok)
        log.info("╠═══════════════════════════════════════════════════════╣")
    log.info("║ E2E TOTAL (runner only) %6.2fs                       ║", t_total)
    log.info("╚═══════════════════════════════════════════════════════╝")
    log.info("")

    # Write machine-readable timing to mws_benchmark_timing.json for the wrapper
    _timing_doc = {
        "runner": {
            "analytics_phases": {k: round(v, 4) for k, v in pt.items()},
            "runner_phases":    {k: round(v, 4) for k, v in rt.items()},
            "token_usage":      tok,
            "prompt_chars":     prompt_chars,
            "total_s":          round(t_total, 4),
        }
    }
    _timing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mws_benchmark_timing.json")
    try:
        if os.path.exists(_timing_path):
            with open(_timing_path, "r", encoding="utf-8") as _f:
                _existing = json.load(_f)
        else:
            _existing = {}
        _existing.update(_timing_doc)
        with open(_timing_path, "w", encoding="utf-8") as _f:
            json.dump(_existing, _f, indent=2)
    except Exception as _e:
        log.debug("Could not write benchmark timing: %s", _e)


def main() -> None:
    _t_main = _time.perf_counter()
    skip_llm = os.environ.get("SKIP_LLM", "false").lower() in ("1", "true", "yes")
    log.info("=== MWS GitHub Runner start | %s | trigger=%s | skip_llm=%s ===",
             TODAY, TRIGGER_REASON, skip_llm)

    try:
        _t0 = _time.perf_counter()
        analytics = run_analytics()
        _RUN_TIMINGS["analytics"] = _time.perf_counter() - _t0
        log.info("Analytics complete — %d candidates, TPV $%.0f",
                 len(analytics["candidates"]), analytics["total_val"])

        # Generate equity curve chart (saved to mws_equity_curve.png)
        _t0 = _time.perf_counter()
        try:
            mws_charts.rotate_and_chart(analytics["df_scores"], analytics["policy"])
            log.info("Chart generated: %s", mws_analytics.CHART_FILENAME)
        except Exception as chart_err:
            log.warning("Chart generation skipped: %s", chart_err)
        _RUN_TIMINGS["chart"] = _time.perf_counter() - _t0

        # Write mws_precomputed_targets.json — skip if the file already covers
        # today's trading date AND holdings content has not changed since last
        # write (set FORCE_RECOMPUTE=1 to override).
        #
        # Content-based checks (not mtime): git operations reset file mtimes to
        # checkout time, making mtime comparisons unreliable in all contexts
        # (GH Actions, local git pull, worktree switches, etc.).
        _force = os.getenv("FORCE_RECOMPUTE", "").strip() in ("1", "true", "yes")
        _tgt_fresh = False
        if not _force and os.path.exists(PRECOMPUTED_TARGETS_FILE):
            try:
                import hashlib as _hl2
                import json as _jc
                with open(PRECOMPUTED_TARGETS_FILE, encoding="utf-8") as _ef:
                    _existing_doc = _jc.load(_ef)
                _stored_date  = _existing_doc.get("run_date", "")
                _stored_hash  = _existing_doc.get("holdings_hash", None)
                _today_td     = mws_analytics._todays_trading_date()
                if os.path.exists(mws_analytics.HOLDINGS_CSV):
                    with open(mws_analytics.HOLDINGS_CSV, "rb") as _chf:
                        _cur_hash = _hl2.md5(_chf.read()).hexdigest()
                else:
                    _cur_hash = ""
                _tgt_fresh = (
                    _stored_date  >= _today_td           # covers today's prices
                    and _stored_hash is not None         # hash was written (new format)
                    and _stored_hash == _cur_hash        # holdings unchanged
                )
            except Exception:
                pass  # any read/parse error → recompute

        _t0 = _time.perf_counter()
        if _tgt_fresh:
            log.info(
                "⚡ %s is up-to-date for %s and holdings unchanged — skipping regeneration "
                "(set FORCE_RECOMPUTE=1 to override)",
                PRECOMPUTED_TARGETS_FILE,
                mws_analytics._todays_trading_date(),
            )
        else:
            try:
                _build_portfolio_tables(analytics)
            except Exception as _pt_err:
                log.warning("Precomputed targets generation skipped: %s", _pt_err)
        _RUN_TIMINGS["portfolio_tables"] = _time.perf_counter() - _t0

        if skip_llm:
            log.info("SKIP_LLM set — sending compliance digest email (no LLM trade proposals).")
            send_compliance_email(analytics)
            _print_benchmark_report(_time.perf_counter() - _t_main)
            return

        _t0 = _time.perf_counter()
        prompt = build_prompt(analytics)
        _RUN_TIMINGS["build_prompt"] = _time.perf_counter() - _t0
        _RUN_TIMINGS["prompt_chars"] = len(prompt)
        log.info("Prompt built (%d chars)", len(prompt))

        _t0 = _time.perf_counter()
        response = call_claude(prompt)
        _RUN_TIMINGS["llm_call"] = _time.perf_counter() - _t0

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
        _print_benchmark_report(_time.perf_counter() - _t_main)

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
