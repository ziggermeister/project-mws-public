#!/usr/bin/env python3
"""
mws_audit.py
────────────
Sends the full MWS codebase to Gemini and/or GPT-4o for logic auditing.

Focuses on the class of bugs where a computation is performed correctly in
isolation but fails to enforce a downstream constraint — e.g. a momentum_buy
recommendation that doesn't verify post-trade sleeve cap compliance.

Usage:
    python3 mws_audit.py                    # both models
    python3 mws_audit.py --model gemini     # Gemini only
    python3 mws_audit.py --model openai     # GPT-4o only
    python3 mws_audit.py --quick            # faster/cheaper — skip policy.json

Outputs:
    mws_audit_results/audit_YYYYMMDD_HHMMSS_gemini.md
    mws_audit_results/audit_YYYYMMDD_HHMMSS_openai.md

API keys required (env vars or .env):
    GEMINI_API_KEY
    OPENAI_API_KEY
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── CLI ───────────────────────────────────────────────────────────────────────
_p = argparse.ArgumentParser(description="MWS codebase logic audit via LLM")
_p.add_argument("--model", choices=["gemini", "openai", "both"], default="both")
_p.add_argument("--quick", action="store_true",
                help="Skip mws_policy.json (saves tokens; focuses on code logic)")
_p.add_argument("--output-dir", default="mws_audit_results",
                help="Directory to write audit markdown files")
args = _p.parse_args()

BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / args.output_dir
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Files to audit ────────────────────────────────────────────────────────────
AUDIT_FILES = [
    ("mws_policy.json",       "Policy — authoritative rule set (caps, floors, signals, gates)"),
    ("mws_analytics.py",      "Analytics engine — momentum scores, gate z-scores, breadth/tactical cash state"),
    ("mws_runner.py",         "Run orchestrator — trade sizing, compliance, deployment, email"),
    ("mws_fetch_history.py",  "Price history fetcher — Stooq incremental fetch, fast-exit logic"),
]
if args.quick:
    AUDIT_FILES = [(f, d) for f, d in AUDIT_FILES if f != "mws_policy.json"]

# ── Audit prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """\
You are an expert quantitative systems auditor reviewing production Python code
for a systematic rules-based portfolio management system called MWS
(Momentum-Weighted Scaling). The system manages a personal taxable/retirement
account with annual SEPP (substantially equal periodic payments) withdrawals.
Bugs have real financial consequences.

Your job is a deep logic audit — not a style review. Focus exclusively on:

1. CONSTRAINT ENFORCEMENT GAPS
   The known bug class: a value is computed correctly in isolation but a
   downstream hard constraint is never checked.
   Confirmed example (already fixed, shown for pattern recognition):
     _est_trade() computed momentum_buy size = abs(target_mv - current_mv)
     without verifying the post-buy sleeve total stays ≤ L2 cap.
     Result: COPX buy recommendation pushed strategic_materials over its 10%
     cap immediately upon execution.
   Look for every analogous pattern where:
   - A buy/sell size is computed without verifying the resulting sleeve/L1/overlay %
   - A compliance check uses different denominators than the sizing calculation
   - A state transition happens without validating the post-transition invariants

2. MULTI-TICKER SLEEVE INTERACTIONS
   When multiple tickers in the same sleeve both receive BUY recommendations,
   does the system correctly share/reduce the available sleeve headroom between
   them, or does each ticker independently assume it has full headroom?
   Check: _est_trade(), DEPLOY loop, compliance_buy sizing.

3. POLICY-CODE CONSISTENCY
   For each rule in mws_policy.json (caps, floors, drawdown thresholds, gate
   sigma values, breadth hysteresis days, turnover caps, SEPP bucket rules),
   verify the corresponding enforcement code in mws_analytics.py and
   mws_runner.py is correct, complete, and uses the right denominator
   (sizing_denom vs compliance_denom vs TPV — they are different).

4. STATE MACHINE CORRECTNESS
   - Breadth hysteresis (pending_days, threshold, floor transitions): are
     edge cases handled (e.g. exactly at threshold, simultaneous floor_exit)?
   - Tactical cash state (filter_blocking, consecutive_blocked_days): are
     transitions correct? Is the known serialization bug (bool not JSON
     serializable) the only issue, or are there logic errors too?
   - Execution gate (buy_defer, sell_defer, spike_trim, stress regime): does
     the gate correctly interact with compliance vs momentum trades?

5. SETTLEMENT / TIMING ASSUMPTIONS
   Sells settle T+1. Does any code path assume same-day availability of sell
   proceeds for buys? Does the deployment (DEPLOY) or compliance_buy logic
   implicitly assume all proceeds are available?

6. EDGE CASES AT BOUNDARIES
   - Ticker at exactly floor_pct or cap_pct (±0.1% band): does the action
     correctly resolve to HOLD, not BUY/TRIM?
   - Sleeve with a single ticker: does sizing divide correctly?
   - All tickers in a sleeve have zero momentum (total_pct = 0): is the
     fallback safe?
   - Drawdown exactly at soft_limit (22%) or hard_limit (30%) boundary.
   - VIX exactly at 28 (re-entry threshold).
   - SEPP Bucket A exactly at $45,000 minimum.

7. OVERLAY / DENOMINATOR INTERACTIONS
   managed_futures (DBMF, KMLM) use TPV as denominator for floor/cap checks
   but sizing_denom (TPV − overlays − Bucket A) for everything else.
   Verify: (a) overlay MV is correctly subtracted from sizing_denom,
   (b) compliance checks for overlays use TPV not sizing_denom,
   (c) the bifurcated compliance_denom (sizing_denom − tactical_cash) is
   applied only to floor/cap checks, not trade sizing.

8. TURNOVER CAP LOGIC (v2.9.9)
   - Compliance buy turnover cap (20% per event): is comp_buy_scale computed
     correctly? Can it inadvertently cap hard_limit emergency buys?
   - Momentum turnover cap (20% per rebalance, 60% annualized): is mom_buy_scale
     applied to the right set of trades? Are spike-trims correctly exempt?

9. FAST-EXIT / CACHING CORRECTNESS
   mws_fetch_history.py and mws_runner.py have content-based fast-exit checks
   (recently fixed from mtime-based). Verify the new checks are correct,
   complete, and handle edge cases (empty file, corrupt JSON, missing hash).

For each finding, provide:
- SEVERITY: P0 (causes incorrect trades) / P1 (silent wrong state) / P2 (minor)
- LOCATION: file name + function name + approximate line range
- DESCRIPTION: what the bug is and under what conditions it triggers
- IMPACT: what goes wrong in the portfolio when it triggers
- FIX: precise code-level fix (pseudocode or actual Python)

If you find no issue in a category, say so explicitly. Do not pad with low-value
observations. End with a prioritized summary table.
"""

def _load_files() -> str:
    """Assemble all audit files into a single string with clear delimiters."""
    parts = []
    for fname, desc in AUDIT_FILES:
        path = BASE_DIR / fname
        if not path.exists():
            parts.append(f"\n{'='*70}\nFILE: {fname} — NOT FOUND\n{'='*70}\n")
            continue
        content = path.read_text(encoding="utf-8")
        parts.append(
            f"\n{'='*70}\n"
            f"FILE: {fname}\n"
            f"DESC: {desc}\n"
            f"LINES: {content.count(chr(10))+1}\n"
            f"{'='*70}\n"
            f"{content}\n"
        )
    return "\n".join(parts)

# ── Gemini ────────────────────────────────────────────────────────────────────
def run_gemini(code_block: str) -> str:
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return "ERROR: google-genai not installed. Run: pip install google-genai"

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "ERROR: GEMINI_API_KEY not set."

    client = genai.Client(api_key=api_key)

    # Prefer deepest reasoning model available on the account
    for model_name in ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"):
        try:
            print(f"  [Gemini] Using model: {model_name}")
            response = client.models.generate_content(
                model=model_name,
                contents=code_block,
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.2,
                    max_output_tokens=65536,
                ),
            )
            return f"# Gemini ({model_name}) Audit\n\n{response.text}"
        except Exception as e:
            print(f"  [Gemini] {model_name} failed: {e} — trying next model")
            time.sleep(2)

    return "ERROR: All Gemini models failed."


# ── OpenAI ────────────────────────────────────────────────────────────────────
def run_openai(code_block: str) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        return "ERROR: openai not installed. Run: pip install openai"

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "ERROR: OPENAI_API_KEY not set."

    client = OpenAI(api_key=api_key)

    # GPT-4o has 128K context. ~6000 lines ≈ 40K-50K tokens — fits fine.
    # Use o1 if available for deeper reasoning on logic bugs.
    for model_name in ("o1", "gpt-4o"):
        try:
            print(f"  [OpenAI] Using model: {model_name}")

            if model_name == "o1":
                # o1 uses a single user message (no system prompt support)
                messages = [{"role": "user", "content": SYSTEM_PROMPT + "\n\n" + code_block}]
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    max_completion_tokens=16000,
                )
            else:
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": code_block},
                ]
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=16000,
                )

            return f"# OpenAI ({model_name}) Audit\n\n{resp.choices[0].message.content}"

        except Exception as e:
            print(f"  [OpenAI] {model_name} failed: {e} — trying next model")
            time.sleep(2)

    return "ERROR: All OpenAI models failed."


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"\nMWS Logic Audit — {ts}")
    print(f"Files: {[f for f, _ in AUDIT_FILES]}")
    print(f"Output: {OUTPUT_DIR}/\n")

    print("Loading source files...")
    code_block = _load_files()
    total_chars = len(code_block)
    est_tokens  = total_chars // 4
    print(f"  Loaded {total_chars:,} chars (~{est_tokens:,} tokens)\n")

    results = {}

    if args.model in ("gemini", "both"):
        print("Running Gemini audit...")
        t0 = time.time()
        results["gemini"] = run_gemini(code_block)
        print(f"  Done in {time.time()-t0:.1f}s\n")

    if args.model in ("openai", "both"):
        print("Running OpenAI audit...")
        t0 = time.time()
        results["openai"] = run_openai(code_block)
        print(f"  Done in {time.time()-t0:.1f}s\n")

    # Write results
    paths = []
    for model_key, content in results.items():
        out_path = OUTPUT_DIR / f"audit_{ts}_{model_key}.md"
        header = (
            f"# MWS Logic Audit — {model_key.upper()}\n"
            f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Files audited:** {', '.join(f for f, _ in AUDIT_FILES)}\n"
            f"**Tokens (est.):** ~{est_tokens:,}\n\n---\n\n"
        )
        out_path.write_text(header + content, encoding="utf-8")
        paths.append(out_path)
        print(f"  Saved: {out_path}")

    print(f"\nAudit complete. {len(paths)} file(s) written.")
    return paths


if __name__ == "__main__":
    main()
