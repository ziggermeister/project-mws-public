#!/bin/bash
set -e

echo "Adding files..."
git add mws_holdings.csv mws_policy.json mws_ticker_history.csv mws_recent_performance.csv .gitignore

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
