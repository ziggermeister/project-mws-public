#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# commit_and_run.sh  —  Commit local file changes and push to main
#
# Purpose:
#   Stages and commits any changed tracked files (holdings, policy, code, etc.)
#   and pushes to main. Does NOT run the MWS pipeline.
#
# To trigger a run after committing, use:
#   python3 trigger_run.py           — fires GitHub Actions (full LLM + email)
#
# Or together:
#   ./commit_and_run.sh && python3 trigger_run.py
#
# Run modes:
#   Mode 1 (automated):   GitHub Actions on schedule
#   Mode 2 (interactive): Ask Claude directly in this session
#   Mode 3 (on-demand):   python3 trigger_run.py
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
  echo "No changes to commit."
else
  echo "Committing..."
  git commit -m "holdings"
  echo "Pushing to main..."
  git push
  echo "Done. To trigger a run: python3 trigger_run.py"
fi
