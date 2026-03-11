#!/usr/bin/env python3
"""
mws_github_runner.py — GitHub Actions orchestrator for MWS portfolio runs.

Runs automatically every Monday (14:00 UTC) via GitHub Actions cron,
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
import smtplib
import sys
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import pandas as pd

# Now safe to import mws_analytics (matplotlib already set to Agg)
import mws_analytics

# ── Constants ──────────────────────────────────────────────────────────────────
TODAY          = datetime.now().strftime("%Y-%m-%d")
TRIGGER_REASON = os.environ.get("TRIGGER_REASON", "scheduled")
# Model is env-configurable — set ANTHROPIC_MODEL secret in GitHub Actions to override.
# claude-3-5-sonnet-20241022 is the default: widely available, supports web_search_20250305.
MODEL          = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-5-20251001"
MAX_TOKENS     = 16000
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
log = logging.getLogger("mws_github_runner")


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

    # Trim policy for prompt — keep news_intelligence and key sections
    policy_trimmed = {k: v for k, v in policy.items() if k not in [
        "ticker_constraints",   # very long, not needed for recommendation
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

Execute the full MWS run protocol as defined in mws_policy.json → news_intelligence.run_protocol.llm_run_sequence:

**STEP 1 (FIRST):** Use the web_search tool to search for current news across all 8 categories in
news_intelligence.categories. Use the exact search queries in news_intelligence.generation_protocol.search_queries.
Rate each item HIGH / MEDIUM / LOW per the materiality_scale rules.

**STEPS 2–8:** Using all inputs below, produce a complete MWS run output including:
- Sleeve-level target weights vs current holdings
- Per-ticker trade recommendations with execution gate assessment
- News overlay per sleeve (confirms / contradicts / novel)
- Override candidates for any HIGH-materiality contradictions
- Final actionable trade list with direction and sizing rationale
- Any proposed policy or code changes (explicit, with rationale — do not silently modify)

Structure your response in two clearly delimited sections:

--- MWS_MARKET_CONTEXT_START ---
# MWS Market Context — AUTO-GENERATED {TODAY}
_Generated by LLM runner. Do not edit manually. Overwritten each run._

[populate all 8 news categories here with items, materiality ratings, and signal interactions]

## Override Candidates
[list any HIGH-materiality contradictions with ticker, direction, rationale]

## Market Regime Snapshot
[VIX level + trend, TPV drawdown from tracker, risk regime assessment]
--- MWS_MARKET_CONTEXT_END ---

--- MWS_RECOMMENDATION_START ---
[full recommendation output per required_recommendation_output format in policy]
--- MWS_RECOMMENDATION_END ---

==============================================================================
## INPUTS
==============================================================================

### POLICY (mws_policy.json — ticker_constraints omitted for brevity)
```json
{json.dumps(policy_trimmed, indent=2, ensure_ascii=False)}
```

### GOVERNANCE RATIONALE (mws_macro.md)
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


# ── Step 4: Parse and write mws_market_context.md ────────────────────────────

def extract_section(text: str, start_tag: str, end_tag: str) -> str:
    if start_tag in text and end_tag in text:
        return text.split(start_tag)[1].split(end_tag)[0].strip()
    return ""

def write_market_context(response_text: str) -> None:
    content = extract_section(
        response_text,
        "--- MWS_MARKET_CONTEXT_START ---",
        "--- MWS_MARKET_CONTEXT_END ---",
    )
    if not content:
        content = (
            f"# MWS Market Context — AUTO-GENERATED {TODAY}\n\n"
            "_Context extraction failed — see full run log in GitHub Actions._\n"
        )
        log.warning("Could not extract MWS_MARKET_CONTEXT section from response.")

    with open(MARKET_CTX_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    log.info("Wrote %s (%d chars)", MARKET_CTX_FILE, len(content))


# ── Step 5: Send email ────────────────────────────────────────────────────────

def send_email(response_text: str) -> None:
    password  = os.environ.get("GMAIL_APP_PASSWORD")
    from_addr = os.environ.get("GMAIL_FROM", "bhatnagar.vivek@gmail.com")
    to_addr   = os.environ.get("GMAIL_TO",   "bhatnagar.vivek@gmail.com")

    if not password:
        log.warning("GMAIL_APP_PASSWORD not set — skipping email. Output committed to repo.")
        return

    recommendation = extract_section(
        response_text,
        "--- MWS_RECOMMENDATION_START ---",
        "--- MWS_RECOMMENDATION_END ---",
    ) or response_text  # fallback: send full response

    subject = f"MWS Run — {TODAY} — {TRIGGER_REASON}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(recommendation, "plain"))

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

        prompt = build_prompt(analytics)
        log.info("Prompt built (%d chars)", len(prompt))

        response = call_claude(prompt)

        write_market_context(response)
        send_email(response)

        log.info("=== MWS GitHub Runner complete ===")

    except Exception as e:
        log.error("Runner failed: %s", e)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
