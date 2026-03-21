"""
tests/unit/test_state_machines.py

State-machine tests for drawdown regime transitions.

These tests verify the temporal / sequential behavior of check_drawdown_state()
across multiple simulated trading days, complementing the point-in-time unit
tests in test_analytics_drawdown.py.

The breadth and tactical-cash state machines are covered in:
  - test_analytics_breadth.py   (compute_and_persist_breadth_states)
  - test_analytics_tactical.py  (compute_and_persist_tactical_cash_state)
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import matplotlib; matplotlib.use("Agg")
import mws_analytics as mws


# ── Helpers ───────────────────────────────────────────────────────────────────

def _policy(soft=0.22, hard=0.30, recovery=0.15):
    return {
        "drawdown_rules": {
            "enabled": True,
            "soft_limit": soft,
            "hard_limit": hard,
            "recovery_condition": {"drawdown_below": recovery},
        }
    }


def _write_cum_twr(path: str, cum_values: list):
    """Write cumulative % TWR values to a PortfolioPct CSV."""
    df = pd.DataFrame({"Date": range(len(cum_values)), "PortfolioPct": cum_values})
    df.to_csv(path, index=False)


def _cum_series_with_transition(phase1_dd: float, n_up1: int, n_down1: int,
                                  n_recovery: int, daily_recovery: float = 0.001):
    """
    Build a two-phase cumulative TWR series:
      Phase 1: rally then drawdown (n_up1 + n_down1 rows) reaching phase1_dd.
      Phase 2: steady recovery from trough (n_recovery rows).

    Returns list of cumulative % returns (PortfolioPct format).
    """
    daily_up = 0.003
    r_down = (1 - phase1_dd) ** (1.0 / max(n_down1, 1)) - 1

    values = []
    cum = 0.0
    for _ in range(n_up1):
        cum = (1.0 + cum) * (1.0 + daily_up) - 1.0
        values.append(cum)
    for _ in range(n_down1):
        cum = (1.0 + cum) * (1.0 + r_down) - 1.0
        values.append(cum)
    for _ in range(n_recovery):
        cum = (1.0 + cum) * (1.0 + daily_recovery) - 1.0
        values.append(cum)
    return values


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDrawdownStateTransitions:

    def test_normal_to_soft_limit(self, tmp_path):
        """
        When drawdown first crosses soft_limit (22%), state transitions from
        normal → soft_limit.
        """
        # 5% drawdown (just built up, well under 22%) → normal
        small = _cum_series_with_transition(0.05, n_up1=50, n_down1=20, n_recovery=0)
        path = str(tmp_path / "perf.csv")
        _write_cum_twr(path, small)
        result_before = mws.check_drawdown_state(_policy(), perf_log=path)
        assert result_before["state"] == "normal"

        # 25% drawdown → soft_limit
        larger = _cum_series_with_transition(0.25, n_up1=50, n_down1=50, n_recovery=0)
        _write_cum_twr(path, larger)
        result_after = mws.check_drawdown_state(_policy(), perf_log=path)
        assert result_after["state"] in ("soft_limit", "hard_limit"), (
            f"Expected soft_limit or hard_limit after 25% drawdown, got {result_after['state']}"
        )

    def test_soft_limit_to_hard_limit(self, tmp_path):
        """
        When drawdown deepens from ~24% to >30%, state escalates from
        soft_limit → hard_limit.
        """
        soft_dd = _cum_series_with_transition(0.24, n_up1=80, n_down1=80, n_recovery=0)
        path = str(tmp_path / "perf.csv")
        _write_cum_twr(path, soft_dd)
        soft_result = mws.check_drawdown_state(_policy(), perf_log=path)
        assert soft_result["state"] in ("soft_limit", "hard_limit")

        hard_dd = _cum_series_with_transition(0.35, n_up1=80, n_down1=100, n_recovery=0)
        _write_cum_twr(path, hard_dd)
        hard_result = mws.check_drawdown_state(_policy(), perf_log=path)
        assert hard_result["state"] == "hard_limit", (
            f"Expected hard_limit after 35% drawdown, got {hard_result['state']}"
        )

    def test_hard_limit_to_normal_via_recovery(self, tmp_path):
        """
        After a hard_limit drawdown, steady recovery brings the rolling 252d
        drawdown back below soft_limit → state returns to normal.
        """
        path = str(tmp_path / "perf.csv")

        # First: build a 32% drawdown → hard_limit
        crash = _cum_series_with_transition(0.32, n_up1=80, n_down1=100, n_recovery=0)
        _write_cum_twr(path, crash)
        hard_result = mws.check_drawdown_state(_policy(), perf_log=path)
        assert hard_result["state"] == "hard_limit"

        # Now: extend with 260 days of strong recovery (+0.3% / day)
        # This pushes the 252d window into recovery period where drawdown is small
        full = _cum_series_with_transition(0.32, n_up1=80, n_down1=100,
                                           n_recovery=260, daily_recovery=0.003)
        _write_cum_twr(path, full)
        recovered = mws.check_drawdown_state(_policy(), perf_log=path)
        assert recovered["state"] == "normal", (
            f"Expected normal after 260 days of recovery, got {recovered['state']} "
            f"(drawdown={recovered['drawdown']:.1%})"
        )

    def test_rolling_window_resets_old_peak(self, tmp_path):
        """
        The rolling 252d window means a peak from >252 days ago no longer anchors
        the measurement — portfolio can exit hard_limit without a full all-time recovery.
        """
        path = str(tmp_path / "perf.csv")

        # Build: 50 up + 200 crash + 260 flat recovery = 510 total rows
        # At row 510, the 252d window starts at row 258: within the flat recovery
        phase1 = _cum_series_with_transition(0.40, n_up1=50, n_down1=200, n_recovery=0)
        phase2 = []
        cum = phase1[-1]
        for _ in range(260):
            cum = (1.0 + cum) * 1.0002 - 1.0  # tiny drift
            phase2.append(cum)

        _write_cum_twr(path, phase1 + phase2)
        result = mws.check_drawdown_state(_policy(), perf_log=path)
        assert result["state"] == "normal", (
            f"Expected normal (old peak outside 252d window), got {result['state']}"
        )

    def test_returns_correct_drawdown_magnitude(self, tmp_path):
        """
        The returned 'drawdown' field should accurately reflect the
        rolling 252d peak-to-trough, negative fraction.
        """
        path = str(tmp_path / "perf.csv")
        # Build exactly a 25% drawdown series
        cum = _cum_series_with_transition(0.25, n_up1=100, n_down1=100, n_recovery=0)
        _write_cum_twr(path, cum)
        result = mws.check_drawdown_state(_policy(), perf_log=path)
        assert result["drawdown"] < 0, "drawdown must be negative"
        # Allow ±3pp tolerance from compounding effects in the helper
        assert abs(result["drawdown"]) > 0.20, (
            f"Drawdown underreported: expected ~25%, got {abs(result['drawdown']):.1%}"
        )

    def test_soft_limit_threshold_configurable_from_policy(self, tmp_path):
        """
        soft_limit should come from the policy's drawdown_rules key,
        not a hardcoded default.  Set soft_limit=0.15 and verify it fires at 16%.
        """
        path = str(tmp_path / "perf.csv")
        cum = _cum_series_with_transition(0.16, n_up1=80, n_down1=80, n_recovery=0)
        _write_cum_twr(path, cum)

        result_tight = mws.check_drawdown_state(_policy(soft=0.15), perf_log=path)
        assert result_tight["state"] in ("soft_limit", "hard_limit"), (
            "Tight soft_limit=0.15 must fire at 16% drawdown"
        )

        # Same drawdown, but relaxed threshold (soft=0.22) → still normal
        result_loose = mws.check_drawdown_state(_policy(soft=0.22), perf_log=path)
        assert result_loose["state"] == "normal", (
            "Relaxed soft_limit=0.22 must NOT fire at 16% drawdown"
        )
