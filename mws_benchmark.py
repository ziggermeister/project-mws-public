#!/usr/bin/env python3
"""
mws_benchmark.py — End-to-end MWS pipeline benchmark.

Runs the full pipeline (fetch → analytics + portfolio tables → optional LLM),
instruments wall-clock time at every phase, counts LLM tokens, and prints a
consolidated benchmark report at the end.

Usage:
    python3 mws_benchmark.py              # fetch + analytics only (SKIP_LLM=true)
    python3 mws_benchmark.py --full       # include LLM call (requires ANTHROPIC_API_KEY)
    python3 mws_benchmark.py --no-fetch   # skip fetch, analytics only (use cached history)

Environment:
    ANTHROPIC_API_KEY   — required for --full mode
    FORCE_RECOMPUTE=1   — force portfolio-table regeneration even if post-close fresh

All timing data is also written to mws_benchmark_timing.json for programmatic use.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent


# ── CLI ───────────────────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="MWS end-to-end benchmark")
_parser.add_argument("--full",     action="store_true", help="Include LLM call (requires ANTHROPIC_API_KEY)")
_parser.add_argument("--no-fetch", action="store_true", help="Skip price fetch (use cached history)")
_parser.add_argument("--days",     type=int, default=60,  help="Days for incremental fetch (default 60)")
_args = _parser.parse_args()


def _run(cmd: list, env: dict = None, label: str = "") -> tuple:
    """
    Run a subprocess, stream its output, and return (wall_s, returncode).
    Timing excludes process spawn overhead (~10ms) but includes all I/O.
    """
    print(f"\n{'━'*60}")
    print(f"▶  {label or ' '.join(cmd)}")
    print(f"{'━'*60}")
    sys.stdout.flush()  # ensure headers appear before subprocess output
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        env=env or os.environ.copy(),
        text=True,
        cwd=str(BASE_DIR),
    )
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        print(f"⚠️  Exit code {proc.returncode}")
    return elapsed, proc.returncode


def _read_timing() -> dict:
    """Load mws_benchmark_timing.json if it exists."""
    p = BASE_DIR / "mws_benchmark_timing.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fmt(seconds: float) -> str:
    if seconds >= 60:
        m = int(seconds // 60)
        s = seconds - m * 60
        return f"{m}m {s:.1f}s"
    return f"{seconds:.2f}s"


def _bar(value: float, max_val: float, width: int = 20) -> str:
    if max_val <= 0:
        return " " * width
    filled = int(round(value / max_val * width))
    return "█" * filled + "░" * (width - filled)


def main():
    wall_times: dict = {}

    # ── Phase 1: Fetch price history ──────────────────────────────────────────
    if not _args.no_fetch:
        fetch_cmd = [sys.executable, "mws_fetch_history.py", "--days", str(_args.days)]
        elapsed, rc = _run(fetch_cmd, label=f"Phase 1: Price fetch (incremental --days {_args.days})")
        wall_times["fetch_wall_s"] = elapsed
        if rc != 0:
            print("⚠️  Fetch exited non-zero — continuing with cached data")
    else:
        print("\n⏭  Phase 1: Fetch skipped (--no-fetch)")
        wall_times["fetch_wall_s"] = 0.0

    # ── Phase 2: Analytics + portfolio tables (SKIP_LLM=true) ─────────────────
    runner_env = os.environ.copy()
    runner_env["SKIP_LLM"]       = "true"
    runner_env["FORCE_RECOMPUTE"] = "1"   # always recompute for benchmark accuracy
    runner_env["TRIGGER_REASON"]  = "benchmark"

    elapsed, rc = _run(
        [sys.executable, "mws_runner.py"],
        env=runner_env,
        label="Phase 2: Analytics + portfolio tables (SKIP_LLM=true, FORCE_RECOMPUTE=1)",
    )
    wall_times["runner_wall_s"] = elapsed

    # ── Phase 3: LLM call (optional) ──────────────────────────────────────────
    if _args.full:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("\n⚠️  --full specified but ANTHROPIC_API_KEY not set — skipping LLM phase")
        else:
            llm_env = os.environ.copy()
            llm_env["TRIGGER_REASON"]  = "benchmark"
            llm_env["FORCE_RECOMPUTE"] = "1"
            elapsed, rc = _run(
                [sys.executable, "mws_runner.py"],
                env=llm_env,
                label="Phase 3: Full run with LLM call",
            )
            wall_times["llm_wall_s"] = elapsed

    # ── Read instrumented timing JSON ──────────────────────────────────────────
    timing = _read_timing()
    fetch  = timing.get("fetch_history", {})
    runner = timing.get("runner", {})
    apt    = runner.get("analytics_phases", {})
    rpt    = runner.get("runner_phases",   {})
    tok    = runner.get("token_usage",     {})

    total_e2e = wall_times.get("fetch_wall_s", 0) + wall_times.get("runner_wall_s", 0)
    if "llm_wall_s" in wall_times:
        total_e2e = wall_times["llm_wall_s"]  # full run already includes analytics

    # ── Print consolidated report ──────────────────────────────────────────────
    max_phase = max(
        fetch.get("parallel_fetch_s", 0),
        apt.get("load_system_files", 0),
        apt.get("update_performance_log", 0),
        apt.get("generate_rankings", 0),
        apt.get("execution_gates", 0),
        rpt.get("chart", 0),
        rpt.get("portfolio_tables", 0),
        rpt.get("llm_call", 0),
        0.01,  # floor so bars always render even when all phases are tiny
    )

    SEP = "═" * 66

    print(f"\n\n{SEP}")
    print(f"  ⏱  MWS END-TO-END BENCHMARK REPORT")
    print(SEP)

    # ── Fetch phase ────────────────────────────────────────────────────────────
    if not _args.no_fetch:
        print(f"\n  PHASE 1 — PRICE FETCH  (wall clock: {_fmt(wall_times['fetch_wall_s'])})")
        print(f"  {'─'*62}")
        if fetch.get("fast_exit"):
            rows = fetch.get("rows_written", "?")
            mode = fetch.get("mode", "incremental (fast-exit)")
            print(f"  ⚡ Fast-exit: history post-close fresh — fetch skipped")
            print(f"     ({rows:,} rows in CSV, {mode})" if isinstance(rows, int) else f"     ({rows} rows in CSV, {mode})")
        else:
            uload  = fetch.get("universe_load_s",   0)
            pfetch = fetch.get("parallel_fetch_s", 0)
            rt     = fetch.get("rt_fallback_s",    0)
            mw     = fetch.get("merge_write_s",    0)
            rows   = fetch.get("rows_written",     "?")
            n_ok   = fetch.get("tickers_ok",       "?")
            n_all  = fetch.get("tickers_fetched",  "?")
            mode   = fetch.get("mode", "incremental")
            print(f"  {'universe load':<28} {_fmt(uload):>8}  {_bar(uload, max_phase)}")
            print(f"  {'parallel fetch':<28} {_fmt(pfetch):>8}  {_bar(pfetch, max_phase)}  ({n_ok}/{n_all} tickers OK)")
            print(f"  {'RT fallback':<28} {_fmt(rt):>8}  {_bar(rt, max_phase)}")
            rows_str = f"{rows:,}" if isinstance(rows, int) else str(rows)
            print(f"  {'merge + write CSV':<28} {_fmt(mw):>8}  {_bar(mw, max_phase)}  ({rows_str} rows, {mode})")

    # ── Analytics phase ────────────────────────────────────────────────────────
    print(f"\n  PHASE 2 — ANALYTICS  (wall clock: {_fmt(wall_times['runner_wall_s'])})")
    print(f"  {'─'*62}")

    analytics_phases = [
        ("load_system_files",         "load_system_files",         "CSV + policy load"),
        ("run_mws_audit",             "run_mws_audit",             "universe audit"),
        ("calculate_portfolio_value", "calculate_portfolio_value", "portfolio value"),
        ("update_performance_log",    "update_performance_log",    "perf log + TWR chain"),
        ("check_drawdown_state",      "check_drawdown_state",      "drawdown state (×2)"),
        ("generate_rankings",         "generate_rankings",         "momentum rankings"),
        ("execution_gates",           "execution_gates",           "execution gates (all tickers)"),
        ("generate_policy_runtime",   "generate_policy_runtime",   "policy runtime strip"),
    ]
    for key, apt_key, label in analytics_phases:
        v = apt.get(apt_key, 0)
        print(f"  {label:<30} {_fmt(v):>8}  {_bar(v, max_phase)}")

    ana_total = rpt.get("analytics", sum(apt.get(k, 0) for _, k, _ in analytics_phases))
    print(f"  {'─'*62}")
    print(f"  {'run_analytics() subtotal':<30} {_fmt(ana_total):>8}")

    runner_phases = [
        ("chart",            "chart generation"),
        ("portfolio_tables", "portfolio tables + targets JSON"),
    ]
    for key, label in runner_phases:
        v = rpt.get(key, 0)
        print(f"  {label:<30} {_fmt(v):>8}  {_bar(v, max_phase)}")

    # ── LLM phase ─────────────────────────────────────────────────────────────
    if _args.full and "llm_wall_s" in wall_times:
        print(f"\n  PHASE 3 — LLM CALL  (wall clock: {_fmt(wall_times['llm_wall_s'])})")
        print(f"  {'─'*62}")
        bp = rpt.get("build_prompt", 0)
        llm = rpt.get("llm_call", 0)
        pchars = rpt.get("prompt_chars", runner.get("prompt_chars", 0))
        est_in_tok = pchars // 4
        print(f"  {'build_prompt':<30} {_fmt(bp):>8}  {_bar(bp, max_phase)}  ({pchars:,} chars ≈ {est_in_tok:,} tokens)")
        print(f"  {'LLM call (API round-trip)':<30} {_fmt(llm):>8}  {_bar(llm, max_phase)}")

        if tok:
            in_t  = tok.get("input_tokens",  0)
            out_t = tok.get("output_tokens", 0)
            cr_t  = tok.get("cache_read_tokens", 0)
            cc_t  = tok.get("cache_create_tokens", 0)
            total_t = in_t + out_t
            tps   = out_t / llm if llm > 0 else 0
            print(f"\n  TOKEN USAGE")
            print(f"  {'─'*62}")
            print(f"  {'input tokens':<30} {in_t:>10,}")
            print(f"  {'output tokens':<30} {out_t:>10,}  ({tps:.0f} tok/s)")
            print(f"  {'cache read tokens':<30} {cr_t:>10,}")
            print(f"  {'cache create tokens':<30} {cc_t:>10,}")
            print(f"  {'TOTAL tokens':<30} {total_t:>10,}")
            # Rough cost estimate: Sonnet input $3/M, output $15/M (public pricing)
            cost_usd = (in_t * 3 + out_t * 15) / 1_000_000
            print(f"  {'Est. cost (Sonnet pricing)':<30} ${cost_usd:>9.4f}")

    # ── E2E summary ────────────────────────────────────────────────────────────
    print(f"\n  {SEP}")
    print(f"  E2E TOTAL (fetch + analytics): {_fmt(total_e2e)}")
    if "llm_wall_s" in wall_times:
        print(f"  E2E TOTAL (with LLM):          {_fmt(total_e2e)}")
    print(f"  {SEP}\n")


if __name__ == "__main__":
    main()
