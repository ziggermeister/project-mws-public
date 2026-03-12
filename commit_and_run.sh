#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# commit_and_run.sh  —  LOCAL workflow only (laptop required)
#
# Purpose:
#   1. Commit any changed files (holdings, policy, ticker history, etc.)
#   2. Push to main
#   3. Run mws_analytics.py locally for charts + diagnostic output
#      (No LLM call, no email — pure local math/diagnostics)
#
# To run a FULL LLM analysis locally (equivalent to GitHub Actions):
#   export ANTHROPIC_API_KEY=...
#   export GMAIL_APP_PASSWORD=... GMAIL_FROM=... GMAIL_TO=...
#   python3 mws_runner.py
#
# Automated cloud runs (no laptop needed):
#   • Daily price fetch:  GitHub Actions weekdays at 21:30 UTC
#   • LLM run:            GitHub Actions weekdays at 14:30 UTC + 21:30 UTC
#   • On-demand LLM run:  GitHub → Actions → "MWS Portfolio Run" → Run workflow
#   • Interactive run:    Ask Claude directly in this session
#
# Usage:
#   ./commit_and_run.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

# ── Stage files ───────────────────────────────────────────────────────────────
echo "Staging files..."
git add \
  mws_holdings.csv \
  mws_policy.json \
  mws_ticker_history.csv \
  mws_recent_performance.csv \
  mws_tracker.json \
  mws_market_context.md \
  mws_run_results.csv \
  mws_governance.md \
  mws_analytics.py \
  mws_charts.py \
  mws_runner.py \
  mws_fetch_history.py \
  mws_llm_run_prompt.md \
  BACKLOG.md \
  requirements.txt \
  .gitignore \
  commit_and_run.sh \
  README.md \
  test_mws.py \
  trigger_run.py \
  .github/workflows/mws_run.yml \
  .github/workflows/mws_daily.yml

# ── Commit if anything changed ────────────────────────────────────────────────
if git diff --cached --quiet; then
  echo "No changes to commit; skipping commit and push."
else
  echo "Committing..."
  git commit -m "holdings"
  echo "Pushing to main..."
  git push
fi

# ── Run local analytics (charts + breach flags) ───────────────────────────────
echo "Running mws_analytics.py (local diagnostics)..."
python3 mws_analytics.py

echo ""
echo "Done. Automated runs:"
echo "  • Daily price fetch:  GitHub Actions weekdays at 21:30 UTC"
echo "  • LLM run:            GitHub Actions weekdays at 14:30 UTC (open+30) and 22:00 UTC (after price fetch)"
echo "  • On-demand LLM run:  GitHub → Actions → MWS Portfolio Run → Run workflow"
echo "  • Local LLM run:      python3 mws_runner.py (requires env vars)"
echo "  • Interactive run:    Ask Claude directly in this session"
