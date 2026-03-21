"""
tests/unit/test_analytics_tactical.py

Tests for mws_analytics.compute_and_persist_tactical_cash_state().

Regression bugs covered:
  Bug #18: tactical_cash_state numpy.bool_ not JSON-serializable.
           The function used pandas boolean series operations that returned
           numpy.bool_ instead of Python bool, which caused json.dump to fail.
"""
import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import matplotlib; matplotlib.use("Agg")
import mws_analytics as mws


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scores_blocking(n_blocking=2):
    """
    Scores DataFrame where n_blocking tickers have Pct >= 0.65 AND RawScore <= 0.
    These are 'would-be momentum buys blocked by abs-filter' → filter_blocking=True.
    """
    rows = [
        {"Ticker": f"BLOCK_{i}", "Pct": 0.80, "RawScore": -0.05}
        for i in range(n_blocking)
    ]
    # Add non-blocking tickers
    rows += [
        {"Ticker": "VTI",  "Pct": 0.50, "RawScore":  0.10},
        {"Ticker": "IAUM", "Pct": 0.30, "RawScore": -0.02},
    ]
    return pd.DataFrame(rows)


def _scores_not_blocking():
    """Scores DataFrame where no ticker triggers the abs-filter block."""
    rows = [
        # High Pct + positive RawScore → would proceed (no blocking)
        {"Ticker": "VTI",  "Pct": 0.80, "RawScore":  0.10},
        # Low Pct + negative RawScore → no buy signal anyway
        {"Ticker": "IAUM", "Pct": 0.20, "RawScore": -0.05},
    ]
    return pd.DataFrame(rows)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestComputeAndPersistTacticalCashState:

    @pytest.mark.regression
    def test_filter_blocking_is_python_bool_not_numpy(self, tmp_path, monkeypatch):
        """
        Bug #18 regression: filter_blocking must be a Python bool, not numpy.bool_.

        numpy.bool_ is not JSON-serializable. The bug caused json.dump to fail
        with 'Object of type bool_ is not JSON serializable'.

        Fix: the code must use bool(...) to convert the pandas result to a Python bool.
        """
        state_path = str(tmp_path / "tactical_cash_state.json")

        df = _scores_blocking()
        result = mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)

        # Check the Python type (not just truthiness)
        assert type(result["filter_blocking"]) is bool, (
            f"Bug #18: filter_blocking type is {type(result['filter_blocking']).__name__}, "
            "expected Python bool (not numpy.bool_). "
            "json.dump will fail with numpy.bool_."
        )

    @pytest.mark.regression
    def test_json_file_is_valid_and_loadable(self, tmp_path, monkeypatch):
        """
        Bug #18 regression: the written JSON must be loadable without error.

        If filter_blocking is numpy.bool_, json.dump raises TypeError and the
        file is either not written or written with a partial/corrupt payload.
        """
        state_path = str(tmp_path / "tactical_cash_state.json")
        df = _scores_blocking()

        # Should not raise
        mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)

        # File must exist and be valid JSON
        assert os.path.exists(state_path), "tactical_cash_state.json was not written"
        with open(state_path) as f:
            loaded = json.load(f)

        assert "filter_blocking" in loaded
        assert "consecutive_blocked_days" in loaded
        assert isinstance(loaded["filter_blocking"], bool), (
            f"Loaded JSON has filter_blocking type {type(loaded['filter_blocking'])}, expected bool"
        )

    def test_blocking_detected_when_high_pct_negative_raw(self, tmp_path):
        """filter_blocking=True when any ticker has Pct >= 0.65 AND RawScore <= 0."""
        state_path = str(tmp_path / "tactical_cash_state.json")
        df = _scores_blocking(n_blocking=1)
        result = mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)
        assert result["filter_blocking"] is True

    def test_not_blocking_when_no_high_pct_negative_raw(self, tmp_path):
        """filter_blocking=False when no ticker has both Pct >= 0.65 AND RawScore <= 0."""
        state_path = str(tmp_path / "tactical_cash_state.json")
        df = _scores_not_blocking()
        result = mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)
        assert result["filter_blocking"] is False

    def test_consecutive_blocked_days_increments(self, tmp_path, monkeypatch):
        """
        Counter increments on each new trading day when blocking persists.

        Because the function uses _todays_trading_date() to detect new days,
        we test by calling once (day 1) then simulating a state file from a
        prior day (prior_date ≠ today) before calling again.
        """
        state_path = str(tmp_path / "tactical_cash_state.json")
        df = _scores_blocking()

        # Call 1 — establishes day 1
        result1 = mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)
        assert result1["consecutive_blocked_days"] >= 1

        # Manually set the state date to a past date to simulate a new day
        with open(state_path) as f:
            state = json.load(f)
        state["date"] = "2000-01-01"  # force stale
        with open(state_path, "w") as f:
            json.dump(state, f)

        # Call 2 — should increment counter
        result2 = mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)
        assert result2["consecutive_blocked_days"] == result1["consecutive_blocked_days"] + 1

    def test_counter_resets_when_blocking_ends(self, tmp_path):
        """Counter resets to 0 when filter_blocking transitions to False."""
        state_path = str(tmp_path / "tactical_cash_state.json")

        # Prime state with blocking=True, count=3, old date
        prior_state = {
            "date":                     "2000-01-01",
            "filter_blocking":          True,
            "consecutive_blocked_days": 3,
        }
        with open(state_path, "w") as f:
            json.dump(prior_state, f)

        # Now call with non-blocking scores
        df = _scores_not_blocking()
        result = mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)
        assert result["filter_blocking"] is False
        assert result["consecutive_blocked_days"] == 0

    def test_same_date_call_is_idempotent(self, tmp_path):
        """Calling twice on the same trading day returns the same state (no double-increment)."""
        state_path = str(tmp_path / "tactical_cash_state.json")
        df = _scores_blocking()

        result1 = mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)
        result2 = mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)

        assert result1["consecutive_blocked_days"] == result2["consecutive_blocked_days"]
        assert result1["filter_blocking"] == result2["filter_blocking"]

    def test_empty_scores_not_blocking(self, tmp_path):
        """Empty scores DataFrame → filter_blocking=False."""
        state_path = str(tmp_path / "tactical_cash_state.json")
        df = pd.DataFrame()
        result = mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)
        assert result["filter_blocking"] is False

    def test_no_tmp_file_left_after_successful_write(self, tmp_path):
        """
        Finding 4 regression: atomic write (.tmp + os.replace) must leave no
        .tmp file behind after a successful write.

        A lingering .tmp file indicates the write was not atomic — os.replace()
        did not run, meaning the state file may be inconsistent.
        """
        state_path = str(tmp_path / "tactical_cash_state.json")
        tmp_path_  = state_path + ".tmp"
        df = _scores_blocking()

        mws.compute_and_persist_tactical_cash_state(df, state_path=state_path)

        assert os.path.exists(state_path), "state file was not written"
        assert not os.path.exists(tmp_path_), (
            f"Atomic write left behind a .tmp file at {tmp_path_}. "
            "os.replace() must clean up the .tmp file on success."
        )
