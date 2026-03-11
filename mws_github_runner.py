#!/usr/bin/env python3
"""
mws_github_runner.py
────────────────────
GitHub Actions orchestrator for fully automated MWS runs.

Runs in the cloud (GitHub's servers) — no laptop required.

Sequence:
  1. Load all input files from repo
  2. Compute analytics summary via mws_analytics.py functions
  3. Call Claude API with full input bundle (policy, macro, holdings,
     tracker, analytics summary, ticker history tail)
  4. Claude performs news search (Step 1 of run protocol) and produces
     full recommendation including mws_market_context.md content
  5. Write mws_market_context.md output back to repo
  6. Append to mws_run_results.csv
  7. Send recommendation email via GAS webhook

Environment variables (set as GitHub Secrets):
  ANTHROPIC_API_KEY     — Anthropic API key (console.anthropic.com)
  GAS_WEBHOOK_URL       — GAS doPost URL for email dispatch
  TRIGGER_REASON        — Why this run fired (set by workflow)

Usage (local test):
  export ANTHROPIC_API_KEY=sk-ant-...
  export GAS_WEBHOOK_URL=https://script.google.com/macros/s/.../exec
  export TRIGGER_REASON=manual_test
  python3 mws_github_runner.py
"""

import json
import os
import sys
import csv
import requests
from datetime import datetime, date
from pathlib import Path
import pandas as pd
import numpy as np

try:
    import anthropic
except ImportError:
    sys.exit("anthropic SDK not installed. Run: pip install anthropic")

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
POLICY_FILE   = ROOT / "mws_policy.json"
MACRO_FILE    = ROOT / "mws_macro.md"
HOLDINGS_FILE = ROOT / "mws_holdings.csv"
TRACKER_FILE  = ROOT / "mws_tracker.json"
HISTORY_FILE  = ROOT / "mws_ticker_history.csv"
RECENT_FILE   = ROOT / "mws_recent_performance.csv"
CONTEXT_FILE  = ROOT / "mws_market_context.md"
RESULTS_FILE  = ROOT / "mws_run_results.csv"

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GAS_WEBHOOK_URL   = os.environ.get("GAS_WEBHOOK_URL", "")
TRIGGER_REASON    = os.environ.get("TRIGGER_REASON", "scheduled")
CLAUDE_MODEL      = "claude-opus-4-5"
HISTORY_TAIL_DAYS = 300   # rows per ticker sent to Claude (enough for all momentum calcs)


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_file(path: Path, label: str) -> str:
    if not path.exists():
        print(f"  WARNING: {label} not found at {path}")
        return f"[{label} not available]"
    content = path.read_text(encoding="utf-8")
    print(f"  ✓ {label} ({len(content):,} chars)")
    return content


def history_tail(path: Path, tail_days: int) -> str:
    """Return last `tail_days` rows per ticker as CSV string."""
    if not path.exists():
        return "[ticker history not available]"
    df = pd.read_csv(path, parse_dates=["Date"])
    df = (df.sort_values(["Ticker", "Date"])
            .groupby("Ticker", group_keys=False)
            .apply(lambda g: g.tail(tail_days)))
    result = df.to_csv(index=False)
    print(f"  ✓ ticker history tail ({len(df):,} rows across {df['Ticker'].nunique()} tickers)")
    return result


def compute_analytics_summary() -> str:
    """
    Run a lightweight analytics pass and return a structured text summary
    for Claude. Avoids matplotlib (no display in Actions) — numbers only.
    """
    try:
        import mws_analytics as mws
    except ImportError:
        return "[mws_analytics import failed — analytics summary unavailable]"

    try:
        policy  = json.loads(POLICY_FILE.read_text())
        state   = json.loads(TRACKER_FILE.read_text())
        hist    = pd.read_csv(HISTORY_FILE, parse_dates=["Date"])
        hold    = pd.read_csv(HOLDINGS_FILE)

        candidates, diag, gate_results = mws.run_mws_audit(policy, state, hist, hold)

        lines = ["=== ANALYTICS SUMMARY (mws_analytics.py) ===\n"]

        # Holdings date
        lines.append(f"Holdings file last modified: {datetime.fromtimestamp(HOLDINGS_FILE.stat().st_mtime).date()}")
        lines.append(f"Ticker history current to: {hist['Date'].max().date()}\n")

        # Rebalance candidates
        if candidates:
            lines.append(f"REBALANCE CANDIDATES ({len(candidates)}):")
            for c in candidates:
                lines.append(f"  {c.get('ticker','?'):8s}  target={c.get('target_weight_pct','?')}%  "
                             f"current={c.get('current_weight_pct','?')}%  "
                             f"action={c.get('action','?')}")
        else:
            lines.append("REBALANCE CANDIDATES: none")

        # Gate results
        if gate_results:
            lines.append(f"\nEXECUTION GATE RESULTS ({len(gate_results)} tickers checked):")
            for g in gate_results:
                clamp = g.get("vol_clamp_type", "none")
                lines.append(f"  {g.get('ticker','?'):8s}  z={g.get('z_score','?'):+.2f}  "
                             f"gate={g.get('gate_action','?'):12s}  "
                             f"vol_clamp={clamp}")

        # Diagnostics
        if diag:
            lines.append(f"\nDIAGNOSTICS:")
            for k, v in diag.items():
                lines.append(f"  {k}: {v}")

        return "\n".join(lines)

    except Exception as e:
        return f"[Analytics summary failed: {type(e).__name__}: {e}]"


def build_prompt(policy: str, macro: str, holdings: str, tracker: str,
                 history_csv: str, recent: str, analytics: str,
                 trigger: str, run_date: str) -> str:
    return f"""You are the MWS portfolio runner executing a full systematic run.

Run date: {run_date}
Trigger: {trigger}

Follow the 8-step run protocol in mws_policy.json → news_intelligence.run_protocol exactly.

STEP 1 is to search the web for current news across all 8 categories defined in
mws_policy.json → news_intelligence.generation_protocol. Rate each item HIGH/MEDIUM/LOW.
Assess signal–news interactions for HIGH items.

After completing all 8 steps, produce your output in TWO clearly separated sections:

━━━ SECTION A: mws_market_context.md ━━━
The full auto-generated market context file content (this will be written back to the repo).
Follow the output_format spec in news_intelligence.generation_protocol exactly.

━━━ SECTION B: MWS RECOMMENDATION ━━━
The full recommendation following the required_recommendation_output format in
mws_policy.json → news_intelligence.required_recommendation_output.
Include:
- holdings_staleness_note: flag if holdings file is >3 trading days old
- sleeve_targets: target weight % per sleeve
- trade_list: specific buy/sell/hold per ticker with rationale
- gate_decisions: defer/execute/spike_trim per ticker with z-score
- news_overlay_summary: how news affected the recommendation
- override_review_candidates: any HIGH-materiality contradictions
- proposed_rule_changes: explicit proposals with rationale (if any)
- proposed_code_changes: explicit proposals with rationale (if any)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INPUT FILES:
────────────────────────────────────────
FILE: mws_policy.json
{policy}

────────────────────────────────────────
FILE: mws_macro.md
{macro}

────────────────────────────────────────
FILE: mws_holdings.csv
{holdings}

────────────────────────────────────────
FILE: mws_tracker.json
{tracker}

────────────────────────────────────────
FILE: mws_recent_performance.csv (last 30 days)
{recent}

────────────────────────────────────────
FILE: mws_ticker_history.csv (last {HISTORY_TAIL_DAYS} rows per ticker)
{history_csv}

────────────────────────────────────────
ANALYTICS SUMMARY (pre-computed by mws_analytics.py)
{analytics}
"""


def parse_sections(response_text: str) -> tuple[str, str]:
    """Split Claude's response into market context and recommendation."""
    ctx_marker  = "SECTION A: mws_market_context.md"
    rec_marker  = "SECTION B: MWS RECOMMENDATION"

    ctx_start = response_text.find(ctx_marker)
    rec_start = response_text.find(rec_marker)

    if ctx_start == -1 or rec_start == -1:
        # Fallback: treat whole response as recommendation
        return "", response_text

    ctx_content = response_text[ctx_start + len(ctx_marker):rec_start].strip("━ \n")
    rec_content = response_text[rec_start + len(rec_marker):].strip("━ \n")
    return ctx_content, rec_content


def write_market_context(content: str, run_date: str) -> None:
    header = (f"# MWS Market Context — AUTO-GENERATED {run_date}\n"
              f"_Generated by LLM runner (GitHub Actions). "
              f"Do not edit manually. Overwritten each run._\n\n")
    CONTEXT_FILE.write_text(header + content, encoding="utf-8")
    print(f"  ✓ mws_market_context.md written ({len(content):,} chars)")


def append_run_results(run_date: str, trigger: str, recommendation_summary: str) -> None:
    file_exists = RESULTS_FILE.exists()
    with open(RESULTS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["run_date", "trigger", "source", "summary"])
        # Truncate recommendation to first 500 chars for the log
        short = recommendation_summary[:500].replace("\n", " ")
        writer.writerow([run_date, trigger, "github_actions", short])
    print(f"  ✓ mws_run_results.csv appended")


def send_via_gas_webhook(subject: str, body: str) -> None:
    if not GAS_WEBHOOK_URL:
        print("  WARNING: GAS_WEBHOOK_URL not set — skipping email dispatch")
        return
    try:
        payload = {"subject": subject, "body": body, "source": "github_actions"}
        resp = requests.post(GAS_WEBHOOK_URL, json=payload, timeout=30)
        resp.raise_for_status()
        print(f"  ✓ Email dispatched via GAS webhook (status {resp.status_code})")
    except Exception as e:
        print(f"  WARNING: GAS webhook failed: {e}")
        print("  (Recommendation is committed to repo — check mws_market_context.md)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not ANTHROPIC_API_KEY:
        sys.exit("ERROR: ANTHROPIC_API_KEY environment variable not set")

    run_date = date.today().isoformat()
    print(f"\n{'='*60}")
    print(f"MWS GitHub Actions Run — {run_date}")
    print(f"Trigger: {TRIGGER_REASON}")
    print(f"{'='*60}\n")

    # ── Load inputs ───────────────────────────────────────────────────────────
    print("Loading input files...")
    policy   = load_file(POLICY_FILE,   "mws_policy.json")
    macro    = load_file(MACRO_FILE,    "mws_macro.md")
    holdings = load_file(HOLDINGS_FILE, "mws_holdings.csv")
    tracker  = load_file(TRACKER_FILE,  "mws_tracker.json")
    recent   = load_file(RECENT_FILE,   "mws_recent_performance.csv")
    hist_csv = history_tail(HISTORY_FILE, HISTORY_TAIL_DAYS)

    print("\nRunning analytics...")
    analytics = compute_analytics_summary()
    print()

    # ── Build prompt ─────────────────────────────────────────────────────────
    prompt = build_prompt(
        policy, macro, holdings, tracker,
        hist_csv, recent, analytics,
        TRIGGER_REASON, run_date
    )
    print(f"Prompt size: {len(prompt):,} chars")

    # ── Call Claude ───────────────────────────────────────────────────────────
    print(f"\nCalling Claude API ({CLAUDE_MODEL})...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = message.content[0].text
    print(f"  ✓ Response received ({len(response_text):,} chars, "
          f"{message.usage.input_tokens:,} in / {message.usage.output_tokens:,} out tokens)")

    # ── Parse and write outputs ───────────────────────────────────────────────
    print("\nParsing response...")
    ctx_content, rec_content = parse_sections(response_text)

    print("Writing outputs...")
    if ctx_content:
        write_market_context(ctx_content, run_date)
    else:
        print("  WARNING: Could not parse Section A — mws_market_context.md not updated")

    append_run_results(run_date, TRIGGER_REASON, rec_content)

    # ── Email ─────────────────────────────────────────────────────────────────
    print("\nDispatching email...")
    subject = f"MWS Run — {run_date} ({TRIGGER_REASON})"
    email_body = f"MWS automated run completed.\n\n{rec_content}"
    send_via_gas_webhook(subject, email_body)

    # ── Done ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"✅  Run complete — {run_date}")
    print(f"    Outputs committed by workflow step (git-auto-commit)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
