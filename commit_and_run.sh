#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# commit_and_run.sh  —  LOCAL workflow only (laptop required)
#
# Purpose:
#   1. Commit any changed files (holdings, policy, ticker history, etc.)
#   2. Push to main
#   3. Run mws_analytics.py locally for charts + diagnostic output
#
# Automated cloud runs (no laptop needed):
#   • Daily price fetch:  GitHub Actions weekdays at 21:30 UTC
#   • Weekly LLM run:     GitHub Actions every Monday at 14:00 UTC
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
  mws_macro.md \
  mws_analytics.py \
  mws_github_runner.py \
  mws_fetch_history.py \
  requirements.txt \
  .gitignore \
  commit_and_run.sh \
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
echo "  • Weekly LLM run:     GitHub Actions every Monday at 14:00 UTC"
echo "  • On-demand LLM run:  GitHub → Actions → MWS Portfolio Run → Run workflow"
echo "  • Interactive run:    Ask Claude directly in this session"
