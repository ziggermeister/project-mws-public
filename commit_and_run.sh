#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# commit_and_run.sh  —  LOCAL workflow (laptop required)
#
# Purpose:
#   1. Commit any changed files (holdings, policy, code, etc.) and push
#   2. Fetch today's prices
#   3. Run full LLM analysis + email  (if ANTHROPIC_API_KEY is set)
#      OR run local analytics only    (if env vars are missing)
#
# Full run requires:
#   export ANTHROPIC_API_KEY=...
#   export GMAIL_APP_PASSWORD=...
#   export GMAIL_FROM=...
#   export GMAIL_TO=...
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

# ── Fetch today's prices (always) ─────────────────────────────────────────────
echo ""
echo "Fetching today's prices..."
python3 mws_fetch_history.py

# ── Full run or local diagnostics ─────────────────────────────────────────────
if [ -n "$ANTHROPIC_API_KEY" ] && [ -n "$GMAIL_APP_PASSWORD" ] && \
   [ -n "$GMAIL_FROM" ] && [ -n "$GMAIL_TO" ]; then
  echo ""
  echo "All env vars present — running full LLM analysis + email..."
  python3 mws_runner.py
else
  echo ""
  echo "ANTHROPIC_API_KEY / Gmail env vars not set — running local analytics only."
  echo "To run a full LLM analysis, export the required env vars and re-run:"
  echo "  export ANTHROPIC_API_KEY=..."
  echo "  export GMAIL_APP_PASSWORD=..."
  echo "  export GMAIL_FROM=..."
  echo "  export GMAIL_TO=..."
  python3 mws_analytics.py
fi

echo ""
echo "Done."
