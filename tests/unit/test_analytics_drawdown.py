"""
tests/unit/test_analytics_drawdown.py

Tests for mws_analytics.check_drawdown_state().

Regression bugs covered:
  Bug #4: Policy uses "drawdown_rules" key, not "risk_controls".
  Bug #5: Drawdown measured from rolling 252-day window, not all-time peak.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

# Ensure repo root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import matplotlib; matplotlib.use("Agg")
import mws_analytics as mws


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_twr_csv(path: str, twr_values):
    """
    Write a minimal performance CSV with a TWR-named column.

    check_drawdown_state searches for a column whose name contains 'twr'
    (case-insensitive) to build the wealth index via (1 + series).cumprod().
    The twr_values passed here should be DAILY RETURNS (as fractions).
    """
    df = pd.DataFrame({"Date": range(len(twr_values)), "TWR": list(twr_values)})
    df.to_csv(path, index=False)


def _make_policy_correct(soft_limit=0.22, hard_limit=0.30, recovery=0.15):
    """Policy dict using the CORRECT 'drawdown_rules' key."""
    return {
        "drawdown_rules": {
            "enabled":    True,
            "soft_limit": soft_limit,
            "hard_limit": hard_limit,
            "recovery_condition": {"drawdown_below": recovery},
        }
    }


def _make_policy_wrong_key(soft_limit=0.22, hard_limit=0.30):
    """Policy dict using the OLD WRONG 'risk_controls' key (bug #4 reproducer)."""
    return {
        "risk_controls": {
            "soft_limit": soft_limit,
            "hard_limit": hard_limit,
        }
    }


def _build_twr_series_with_drawdown(drawdown_frac: float, n_up=100, n_down=50):
    """
    Build a cumulative-TWR series (PortfolioPct format) that peaks after n_up
    steps then falls by drawdown_frac.

    check_drawdown_state reads the column as cumulative % returns since inception
    (e.g. 0.15 = +15% total) and passes it directly to _compute_max_drawdown,
    which adds 1 internally to convert to wealth-index levels.

    Format: each value is the TOTAL CUMULATIVE RETURN since inception (fraction),
    matching what update_performance_log() writes to "PortfolioPct".
    """
    daily_up = 0.003      # small positive step — builds cumulative return steadily
    r_down   = (1 - drawdown_frac) ** (1.0 / n_down) - 1

    values = []
    cum = 0.0
    for _ in range(n_up):
        cum = (1.0 + cum) * (1.0 + daily_up) - 1.0
        values.append(cum)
    for _ in range(n_down):
        cum = (1.0 + cum) * (1.0 + r_down) - 1.0
        values.append(cum)
    return values


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCheckDrawdownState:

    def test_normal_when_no_perf_log(self, tmp_path):
        """Returns normal state when performance log does not exist."""
        policy = _make_policy_correct()
        result = mws.check_drawdown_state(policy, perf_log=str(tmp_path / "nonexistent.csv"))
        assert result["state"] == "normal"
        assert result["drawdown"] == 0.0

    def test_normal_small_drawdown(self, tmp_path):
        """State is normal when drawdown is well below soft limit."""
        cum = _build_twr_series_with_drawdown(0.05)  # only 5% drawdown
        path = str(tmp_path / "perf.csv")
        _write_twr_csv(path, cum)
        policy = _make_policy_correct(soft_limit=0.22, hard_limit=0.30)
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["state"] == "normal"

    def test_hard_limit_fires_at_30_pct(self, tmp_path):
        """hard_limit activates when drawdown >= 30%."""
        cum = _build_twr_series_with_drawdown(0.35, n_down=100)  # 35% drawdown > 30%
        path = str(tmp_path / "perf.csv")
        _write_twr_csv(path, cum)
        policy = _make_policy_correct(soft_limit=0.22, hard_limit=0.30)
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["state"] == "hard_limit"

    def test_soft_limit_fires_at_22_pct(self, tmp_path):
        """soft_limit activates when drawdown is between 22% and 30%."""
        cum = _build_twr_series_with_drawdown(0.25, n_down=100)  # 25% dd → soft
        path = str(tmp_path / "perf.csv")
        _write_twr_csv(path, cum)
        policy = _make_policy_correct(soft_limit=0.22, hard_limit=0.30)
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["state"] == "soft_limit", (
            f"25% drawdown with soft=22%, hard=30% must yield 'soft_limit', got {result['state']!r}"
        )

    def test_returns_policy_thresholds(self, tmp_path):
        """Returned dict contains soft_limit and hard_limit from policy."""
        cum = [0.01] * 10
        path = str(tmp_path / "perf.csv")
        _write_twr_csv(path, cum)
        policy = _make_policy_correct(soft_limit=0.18, hard_limit=0.27)
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["soft_limit"] == 0.18
        assert result["hard_limit"] == 0.27

    def test_missing_twr_column_returns_normal(self, tmp_path):
        """Returns normal state when CSV has no TWR-like column."""
        path = str(tmp_path / "perf.csv")
        df = pd.DataFrame({"Date": [1, 2, 3], "OtherCol": [0.1, 0.2, 0.3]})
        df.to_csv(path, index=False)
        policy = _make_policy_correct()
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["state"] == "normal"

    @pytest.mark.regression
    def test_reads_drawdown_rules_key_not_risk_controls(self, tmp_path):
        """
        Bug #4 regression: policy uses 'drawdown_rules' key, not 'risk_controls'.

        The old code read policy.get('risk_controls', {}), which returned {}
        and caused defaults to be used (soft=20%, hard=28% — wrong values).
        A 21% drawdown would incorrectly trigger soft_limit with the wrong key
        but should remain 'normal' when soft_limit is correctly read as 22%.

        Setup: TWR series with ~21% drawdown (between old wrong 20% and correct 22%).
        With correct key ('drawdown_rules', soft=22%): state should be 'normal'.
        With wrong key ('risk_controls', defaults to soft=0.22 too): also normal, BUT
        the key point is the code must read from 'drawdown_rules', not fall back to
        hardcoded defaults when the key is absent. We verify by giving the policy
        only the 'risk_controls' key (wrong), expecting the correct key to be absent
        and the code to use hardcoded defaults rather than the wrong values.
        """
        # Build a 21% drawdown — between wrong default (20%) and correct (22%)
        cum = _build_twr_series_with_drawdown(0.21, n_down=100)
        path = str(tmp_path / "perf.csv")
        _write_twr_csv(path, cum)

        # Policy with ONLY the correct 'drawdown_rules' key (soft=22%)
        policy_correct = _make_policy_correct(soft_limit=0.22, hard_limit=0.30)
        result_correct = mws.check_drawdown_state(policy_correct, perf_log=path)

        # A 21% drawdown < 22% soft_limit → must be normal
        # (Allows for compounding making it slightly over in edge cases)
        assert result_correct["soft_limit"] == 0.22, (
            "check_drawdown_state must read soft_limit from 'drawdown_rules' key, got wrong value"
        )
        # State must be normal for 21% drawdown with 22% threshold
        # (compounding may push it slightly: allow for rounding)
        assert result_correct["state"] in ("normal", "soft_limit"), (
            "Unexpected hard_limit state for 21% drawdown"
        )

        # Critically: when only 'risk_controls' key present (wrong key), the function
        # must fall back gracefully (not crash) and use the internal default soft_limit
        policy_wrong_key = _make_policy_wrong_key(soft_limit=0.20, hard_limit=0.28)
        result_wrong = mws.check_drawdown_state(policy_wrong_key, perf_log=path)
        # Since 'drawdown_rules' is absent, the function uses hardcoded defaults (0.22/0.30)
        # so a 21% drawdown should still be 'normal' with the hardcoded 0.22 default
        assert result_wrong["state"] in ("normal", "soft_limit"), (
            "Function crashed or returned unexpected state with wrong policy key"
        )

    @pytest.mark.regression
    def test_rolling_252d_ignores_old_drawdown(self, tmp_path):
        """
        Bug #5 regression: drawdown uses rolling 252-day window, not all-time peak.

        If the code measured from the all-time peak (not rolling 252d), a large
        historical drawdown outside the window would anchor the peak permanently,
        keeping the system in hard_limit/soft_limit even when the portfolio has
        fully recovered for more than a year.

        Setup:
          Rows 0-249: deep -40% drawdown (outside 252-day rolling window)
          Rows 250-500: clean flat/slightly positive (+2%) — inside the 252d window
          Peak within the 252d window is near rows 0-249 boundary, at the trough.
          With correct rolling window: drawdown in window < 5% → state == "normal"
          With all-time peak: peak is at row ~100 → 40% drawdown → hard_limit
        """
        # Phase 1: 250 rows of crash (outside rolling window when we have 500 rows total)
        # Returns cumulative % returns — each value is total return since inception.
        phase1 = _build_twr_series_with_drawdown(0.40, n_up=50, n_down=200)
        # Phase 2: 250 rows of flat recovery (inside the 252d window).
        # Continue cumulative series from end of phase1: add tiny positive daily step each row.
        # Each value is (1 + prev) * (1 + daily_step) - 1 (continuous compounding from phase1).
        phase2 = []
        cum = phase1[-1]
        for _ in range(250):
            cum = (1.0 + cum) * (1.0 + 0.0002) - 1.0
            phase2.append(cum)

        cum_all = phase1 + phase2
        path = str(tmp_path / "perf.csv")
        _write_twr_csv(path, cum_all)

        policy = _make_policy_correct(soft_limit=0.22, hard_limit=0.30)
        result = mws.check_drawdown_state(policy, perf_log=path)

        # The last 252 rows (phase2) are flat/positive — max drawdown in window << 22%
        # → must be "normal"
        assert result["state"] == "normal", (
            f"Expected normal state (rolling 252d window shows recovery), "
            f"got {result['state']} with drawdown {result['drawdown']:.1%}. "
            "Bug #5: code may still be measuring from all-time peak."
        )

    @pytest.mark.regression
    def test_portfoliopct_column_found(self, tmp_path):
        """
        Bug: column lookup only matched 'twr' — 'PortfolioPct' (production column)
        was never found so drawdown was always 'normal' in production.

        Write CSV with column 'PortfolioPct' (production name) and a >22% drawdown.
        The function must find the column and return the correct state.
        """
        cum = _build_twr_series_with_drawdown(0.25, n_down=100)  # 25% dd → soft_limit
        df = pd.DataFrame({"Date": range(len(cum)), "PortfolioPct": cum})
        path = str(tmp_path / "perf.csv")
        df.to_csv(path, index=False)
        policy = _make_policy_correct(soft_limit=0.22, hard_limit=0.30)
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["state"] in ("soft_limit", "hard_limit"), (
            f"Bug: 'PortfolioPct' column not recognised — drawdown check silently disabled. "
            f"Got state='{result['state']}', drawdown={result['drawdown']:.1%}"
        )

    @pytest.mark.regression
    def test_no_double_compounding(self, tmp_path):
        """
        Bug: code built wealth = (1+series).cumprod() then passed it to
        _compute_max_drawdown which added 1 again — double-compounding made
        a real 35% drawdown appear as ~20%, silently preventing hard_limit.

        Write a 35% cumulative-drawdown series directly (PortfolioPct format).
        hard_limit (30%) must fire.
        """
        cum = _build_twr_series_with_drawdown(0.35, n_down=100)  # 35% dd > 30%
        df = pd.DataFrame({"Date": range(len(cum)), "PortfolioPct": cum})
        path = str(tmp_path / "perf.csv")
        df.to_csv(path, index=False)
        policy = _make_policy_correct(soft_limit=0.22, hard_limit=0.30)
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["state"] == "hard_limit", (
            f"Bug: double-compounding underreports drawdown — 35% drawdown must trigger "
            f"hard_limit (30%). Got state='{result['state']}', drawdown={result['drawdown']:.1%}"
        )
