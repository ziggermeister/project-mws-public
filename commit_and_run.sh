#!/bin/bash

set -e  # stop on first error

echo "Adding files..."
git add mws_holdings.csv mws_policy.json mws_ticker_history.csv mws_recent_performance.csv .gitignore

echo "Committing..."
git commit -m "holdings"

echo "Pushing to remote..."
git push

echo "Running Titanium runner..."
python3 mws_titanium_runner.py

echo "Done."
