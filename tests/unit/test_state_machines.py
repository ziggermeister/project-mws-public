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


# ── Tests: update_and_check_drawdown_state (ITEM 1) ───────────────────────────

def _make_scores_df(vti_raw=0.0):
    """Build a minimal df_scores with VTI at a given RawScore."""
    return pd.DataFrame([{
        "Ticker":   "VTI",
        "Score":    0.5,
        "Pct":      0.5,
        "RawScore": vti_raw,
        "Alpha":    "N/A",
        "AlphaVs":  "VTI",
        "Sleeve":   "core_equity",
        "Status":   "INDUCTED/HELD",
    }])


def _perf_path_with_drawdown(tmp_path, drawdown_pct, name="perf.csv"):
    """Write a performance CSV that yields the specified drawdown and return path."""
    path = str(tmp_path / name)
    cum = _cum_series_with_transition(drawdown_pct, n_up1=80, n_down1=80, n_recovery=0)
    _write_cum_twr(path, cum)
    return path


class TestDrawdownRecoveryStateMachine:
    """
    Tests for update_and_check_drawdown_state() — the stateful, persisted
    version of check_drawdown_state() that tracks recovery counters.
    """

    def test_no_state_file_returns_clean_defaults(self, tmp_path):
        """No JSON file → function returns state with counters at 0."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")
        # Normal state (tiny drawdown)
        _write_cum_twr(perf_path, _cum_series_with_transition(0.05, 50, 20, 0))

        result = mws.update_and_check_drawdown_state(
            _policy(), perf_path, pd.DataFrame(), state_path=state_path
        )
        assert result["consecutive_days_recovered"] == 0
        assert result["vti_pos_mom_days"] == 0
        assert "state" in result

    def test_normal_state_keeps_counters_zero(self, tmp_path):
        """In normal state (no drawdown): both counters stay 0."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")
        _write_cum_twr(perf_path, _cum_series_with_transition(0.05, 50, 20, 0))

        result = mws.update_and_check_drawdown_state(
            _policy(), perf_path, _make_scores_df(vti_raw=0.5), state_path=state_path
        )
        assert result["state"] == "normal"
        assert result["consecutive_days_recovered"] == 0
        assert result["vti_pos_mom_days"] == 0

    def test_stress_below_threshold_increments_consecutive_days(self, tmp_path, monkeypatch):
        """soft_limit state + drawdown < 15% → consecutive_days_recovered increments."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")
        # 24% drawdown → soft_limit
        _write_cum_twr(perf_path, _cum_series_with_transition(0.24, 80, 80, 0))

        # Monkeypatch check_drawdown_state to return soft_limit with dd < 0.15
        monkeypatch.setattr(mws, "check_drawdown_state", lambda policy, perf_log: {
            "state": "soft_limit", "drawdown": -0.10,  # 10% < 15% threshold
            "soft_limit": 0.22, "hard_limit": 0.30, "recovery": 0.15,
        })

        result = mws.update_and_check_drawdown_state(
            _policy(), perf_path, _make_scores_df(vti_raw=-0.1), state_path=state_path
        )
        assert result["consecutive_days_recovered"] == 1, (
            "First day below recovery threshold in soft_limit should set counter to 1"
        )

    def test_stress_above_threshold_resets_consecutive_days(self, tmp_path, monkeypatch):
        """soft_limit state + drawdown > 15% → counter stays 0."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")

        monkeypatch.setattr(mws, "check_drawdown_state", lambda policy, perf_log: {
            "state": "soft_limit", "drawdown": -0.20,  # 20% > 15% threshold
            "soft_limit": 0.22, "hard_limit": 0.30, "recovery": 0.15,
        })

        result = mws.update_and_check_drawdown_state(
            _policy(), perf_path, _make_scores_df(vti_raw=-0.1), state_path=state_path
        )
        assert result["consecutive_days_recovered"] == 0

    def test_recovery_triggers_at_10_consecutive_days(self, tmp_path, monkeypatch):
        """10 consecutive days with dd < 0.15 in soft_limit → recovery_triggered=True."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")

        monkeypatch.setattr(mws, "check_drawdown_state", lambda policy, perf_log: {
            "state": "soft_limit", "drawdown": -0.10,
            "soft_limit": 0.22, "hard_limit": 0.30, "recovery": 0.15,
        })

        _dates = [f"2026-03-{10+i:02d}" for i in range(11)]
        result = None
        for i, d in enumerate(_dates):
            monkeypatch.setattr(mws, "_todays_trading_date", lambda _d=d: _d)
            result = mws.update_and_check_drawdown_state(
                _policy(), perf_path, _make_scores_df(vti_raw=-0.1), state_path=state_path
            )

        assert result["recovery_triggered"] is True, (
            "After 10+ consecutive days below threshold, recovery_triggered must be True"
        )
        assert result["state"] == "normal", "State must flip to normal on recovery"

    def test_vti_positive_increments_vti_counter(self, tmp_path, monkeypatch):
        """In soft_limit with positive VTI RawScore → vti_pos_mom_days increments."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")

        monkeypatch.setattr(mws, "check_drawdown_state", lambda policy, perf_log: {
            "state": "soft_limit", "drawdown": -0.25,
            "soft_limit": 0.22, "hard_limit": 0.30, "recovery": 0.15,
        })

        result = mws.update_and_check_drawdown_state(
            _policy(), perf_path, _make_scores_df(vti_raw=0.3), state_path=state_path
        )
        assert result["vti_pos_mom_days"] == 1, (
            "Positive VTI RawScore in soft_limit should set vti_pos_mom_days to 1"
        )

    def test_vti_recovery_triggers_at_5_days(self, tmp_path, monkeypatch):
        """5 consecutive days with positive VTI → state=normal on day 5."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")

        monkeypatch.setattr(mws, "check_drawdown_state", lambda policy, perf_log: {
            "state": "soft_limit", "drawdown": -0.25,
            "soft_limit": 0.22, "hard_limit": 0.30, "recovery": 0.15,
        })

        _dates = [f"2026-03-{10+i:02d}" for i in range(5)]
        result = None
        for d in _dates:
            monkeypatch.setattr(mws, "_todays_trading_date", lambda _d=d: _d)
            result = mws.update_and_check_drawdown_state(
                _policy(), perf_path, _make_scores_df(vti_raw=0.4), state_path=state_path
            )

        assert result["recovery_triggered"] is True, (
            "5 consecutive days of positive VTI momentum must trigger recovery"
        )
        assert result["state"] == "normal"

    def test_vti_counter_resets_on_negative(self, tmp_path, monkeypatch):
        """VTI goes negative mid-streak → counter resets to 0."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")

        monkeypatch.setattr(mws, "check_drawdown_state", lambda policy, perf_log: {
            "state": "soft_limit", "drawdown": -0.25,
            "soft_limit": 0.22, "hard_limit": 0.30, "recovery": 0.15,
        })

        # Day 1: positive VTI
        monkeypatch.setattr(mws, "_todays_trading_date", lambda: "2026-03-10")
        mws.update_and_check_drawdown_state(
            _policy(), perf_path, _make_scores_df(vti_raw=0.4), state_path=state_path
        )

        # Day 2: negative VTI
        monkeypatch.setattr(mws, "_todays_trading_date", lambda: "2026-03-11")
        result = mws.update_and_check_drawdown_state(
            _policy(), perf_path, _make_scores_df(vti_raw=-0.2), state_path=state_path
        )
        assert result["vti_pos_mom_days"] == 0, "Negative VTI must reset vti_pos_mom_days to 0"

    def test_idempotent_on_same_day(self, tmp_path, monkeypatch):
        """Call twice with same date → counters do not double-increment."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")

        monkeypatch.setattr(mws, "check_drawdown_state", lambda policy, perf_log: {
            "state": "soft_limit", "drawdown": -0.10,
            "soft_limit": 0.22, "hard_limit": 0.30, "recovery": 0.15,
        })
        monkeypatch.setattr(mws, "_todays_trading_date", lambda: "2026-03-15")

        mws.update_and_check_drawdown_state(
            _policy(), perf_path, _make_scores_df(vti_raw=0.3), state_path=state_path
        )
        result = mws.update_and_check_drawdown_state(
            _policy(), perf_path, _make_scores_df(vti_raw=0.3), state_path=state_path
        )
        # Both counters should be 1 (from first call), not 2 (idempotent)
        assert result["consecutive_days_recovered"] <= 1
        assert result["vti_pos_mom_days"] <= 1

    def test_no_tmp_file_left_after_write(self, tmp_path, monkeypatch):
        """Atomic write must not leave a .tmp file behind."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")
        _write_cum_twr(perf_path, _cum_series_with_transition(0.05, 50, 20, 0))

        mws.update_and_check_drawdown_state(
            _policy(), perf_path, pd.DataFrame(), state_path=state_path
        )
        assert not os.path.exists(state_path + ".tmp"), (
            "Atomic write must clean up the .tmp file after os.replace"
        )

    def test_state_file_has_required_keys(self, tmp_path):
        """Output JSON must contain all required keys."""
        state_path = str(tmp_path / "dd_state.json")
        perf_path  = str(tmp_path / "perf.csv")
        _write_cum_twr(perf_path, _cum_series_with_transition(0.05, 50, 20, 0))

        result = mws.update_and_check_drawdown_state(
            _policy(), perf_path, pd.DataFrame(), state_path=state_path
        )
        required_keys = {
            "date", "state", "drawdown",
            "consecutive_days_recovered", "vti_pos_mom_days", "recovery_triggered",
        }
        missing = required_keys - set(result.keys())
        assert not missing, f"Missing keys in drawdown state: {missing}"
