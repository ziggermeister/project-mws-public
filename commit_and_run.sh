#!/bin/bash
set -e

echo "Syncing Google Apps Script (browser -> local)..."
if [ -f "daily-emailer/.clasp.json" ]; then
  (cd daily-emailer && clasp pull)
else
  echo "WARNING: daily-emailer/.clasp.json not found; skipping clasp pull."
fi

echo "Adding files..."
git add \
  mws_holdings.csv \
  mws_policy.json \
  mws_ticker_history.csv \
  mws_recent_performance.csv \
  .gitignore \
  daily-emailer/appsscript.json \
  daily-emailer/ContractValidator.js \
  daily-emailer/DailyEmailRunner.js \
  daily-emailer/VnextContract.js \
  commit_and_run.sh \
  mws_titanium_runner.py \
  mws_macro.md \
  mws_tracker.json

# If git add produced no staged changes, skip commit/push but still run python
if git diff --cached --quiet; then
  echo "No changes to commit; skipping commit and push."
else
  echo "Committing..."
  git commit -m "holdings"

  echo "Pushing to remote..."
  git push
fi

echo "Running Titanium runner..."
python3 mws_titanium_runner.py

echo "Done."
