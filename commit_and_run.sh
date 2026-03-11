#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# commit_and_run.sh  —  LOCAL workflow only (laptop required)
#
# Purpose:
#   1. [Optional] Sync GAS code from browser → local via CLASP
#      Run with --clasp flag when you've edited GAS code in the browser
#      and want those changes committed to git.
#   2. Commit any changed files (holdings, policy, ticker history, etc.)
#   3. Push to main
#   4. Run mws_analytics.py locally for charts + diagnostic output
#
# Automated cloud run (no laptop needed):
#   GitHub Actions handles the full LLM run automatically.
#   See .github/workflows/mws_run.yml
#   Trigger manually: GitHub → Actions → "MWS Portfolio Run" → Run workflow
#
# Usage:
#   ./commit_and_run.sh           # commit + push + run analytics
#   ./commit_and_run.sh --clasp   # sync GAS from browser first, then above
# ─────────────────────────────────────────────────────────────────────────────
set -e

# ── Optional CLASP sync (only when --clasp flag passed) ──────────────────────
if [[ "$1" == "--clasp" ]]; then
  echo "Syncing Google Apps Script (browser -> local via CLASP)..."
  if [ -f "daily-emailer/.clasp.json" ]; then
    (cd daily-emailer && clasp pull)
    echo "  ✓ CLASP pull complete"
  else
    echo "  WARNING: daily-emailer/.clasp.json not found; skipping CLASP pull."
  fi
else
  echo "(Skipping CLASP — pass --clasp flag if you edited GAS code in browser)"
fi

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
  daily-emailer/appsscript.json \
  daily-emailer/ContractValidator.js \
  daily-emailer/DailyEmailRunner.js \
  daily-emailer/VnextContract.js \
  .github/workflows/mws_run.yml

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
echo "Done. To trigger a full LLM run without laptop:"
echo "  • Automated: GitHub Actions fires every Monday at 14:00 UTC"
echo "  • On-demand: GitHub → Actions → MWS Portfolio Run → Run workflow"
