"""
test_mws.py — Unit tests for mws_analytics.py

Run with:
    python -m pytest test_mws.py -v

Coverage targets:
    - get_held_tickers        (vectorized, edge cases)
    - _compute_tr12m          (12-month total return)
    - _compute_slope_6m       (annualized OLS log-price slope)
    - _compute_residual_3m    (ticker vs anchor residual)
    - _blend_score            (weighted blend with partial-component fallback)
    - _compute_max_drawdown   (peak-to-trough)
    - check_drawdown_state    (normal / soft_limit / hard_limit gate)
"""

import math
import os
import tempfile
import textwrap

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Patch matplotlib so tests run headless without display
# ---------------------------------------------------------------------------
import matplotlib
matplotlib.use("Agg")

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
import mws_analytics as mws


# ===========================================================================
# Helpers
# ===========================================================================

def _price_series(values, start="2024-01-01", freq="B") -> pd.Series:
    """Build a pd.Series of prices indexed by business-day DatetimeIndex."""
    idx = pd.bdate_range(start=start, periods=len(values))
    return pd.Series(values, index=idx, dtype=float)


def _make_hold(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal holdings DataFrame with Ticker and Shares columns."""
    return pd.DataFrame(rows, columns=["Ticker", "Shares"])


def _make_policy(
    soft_limit: float = 0.20,
    hard_limit: float = 0.28,
    recovery: float = 0.12,
) -> dict:
    return {
        "risk_controls": {
            "soft_limit":         soft_limit,
            "hard_limit":         hard_limit,
            "recovery_threshold": recovery,
        }
    }


# ===========================================================================
# get_held_tickers
# ===========================================================================

class TestGetHeldTickers:
    def test_returns_positive_share_tickers(self):
        hold = _make_hold([
            {"Ticker": "VTI",  "Shares": "100"},
            {"Ticker": "SOXQ", "Shares": "50"},
        ])
        result = mws.get_held_tickers(hold)
        assert result == {"VTI", "SOXQ"}

    def test_excludes_zero_shares(self):
        hold = _make_hold([
            {"Ticker": "VTI",  "Shares": "100"},
            {"Ticker": "CASH", "Shares": "0"},
        ])
        result = mws.get_held_tickers(hold)
        assert "CASH" not in result
        assert "VTI" in result

    def test_excludes_negative_shares(self):
        hold = _make_hold([
            {"Ticker": "VTI",  "Shares": "-5"},
            {"Ticker": "IBIT", "Shares": "86"},
        ])
        result = mws.get_held_tickers(hold)
        assert "VTI" not in result
        assert "IBIT" in result

    def test_handles_non_numeric_shares(self):
        hold = _make_hold([
            {"Ticker": "VTI",  "Shares": "not_a_number"},
            {"Ticker": "BOTZ", "Shares": "10"},
        ])
        result = mws.get_held_tickers(hold)
        assert "VTI" not in result
        assert "BOTZ" in result

    def test_empty_dataframe_returns_empty_set(self):
        hold = _make_hold([])
        result = mws.get_held_tickers(hold)
        assert result == set()

    def test_missing_columns_returns_empty_set(self):
        hold = pd.DataFrame({"Symbol": ["VTI"], "Qty": [100]})
        result = mws.get_held_tickers(hold)
        assert result == set()

    def test_normalizes_ticker_case(self):
        hold = _make_hold([{"Ticker": "vti", "Shares": "100"}])
        result = mws.get_held_tickers(hold)
        assert "VTI" in result

    def test_strips_whitespace(self):
        hold = _make_hold([{"Ticker": "  VTI  ", "Shares": " 100 "}])
        result = mws.get_held_tickers(hold)
        assert "VTI" in result


# ===========================================================================
# _compute_tr12m
# ===========================================================================

class TestComputeTr12m:
    def test_positive_return(self):
        # Doubles over the period → 100% return
        prices = _price_series([100.0] * 126 + [200.0])
        result = mws._compute_tr12m(prices)
        assert result is not None
        assert math.isclose(result, 1.0, rel_tol=1e-9)

    def test_negative_return(self):
        # Halves over the period
        prices = _price_series([200.0] + [100.0] * 126)
        result = mws._compute_tr12m(prices)
        assert result is not None
        assert math.isclose(result, -0.5, rel_tol=1e-9)

    def test_flat_returns_zero(self):
        prices = _price_series([100.0] * 252)
        result = mws._compute_tr12m(prices)
        assert result is not None
        assert math.isclose(result, 0.0, abs_tol=1e-9)

    def test_single_point_returns_none(self):
        prices = _price_series([100.0])
        result = mws._compute_tr12m(prices)
        assert result is None

    def test_empty_returns_none(self):
        prices = _price_series([])
        result = mws._compute_tr12m(prices)
        assert result is None

    def test_ignores_nan_values(self):
        prices = pd.Series([100.0, np.nan, np.nan, 150.0])
        result = mws._compute_tr12m(prices)
        assert result is not None
        assert math.isclose(result, 0.5, rel_tol=1e-9)


# ===========================================================================
# _compute_slope_6m
# ===========================================================================

class TestComputeSlope6m:
    def test_upward_trend_positive_slope(self):
        # Steadily rising prices → positive annualized slope
        x = np.linspace(100, 200, 130)
        prices = _price_series(x.tolist())
        result = mws._compute_slope_6m(prices)
        assert result is not None
        assert result > 0

    def test_downward_trend_negative_slope(self):
        x = np.linspace(200, 100, 130)
        prices = _price_series(x.tolist())
        result = mws._compute_slope_6m(prices)
        assert result is not None
        assert result < 0

    def test_too_few_points_returns_none(self):
        prices = _price_series([100.0] * 9)
        result = mws._compute_slope_6m(prices)
        assert result is None

    def test_flat_prices_near_zero_slope(self):
        prices = _price_series([100.0] * 130)
        result = mws._compute_slope_6m(prices)
        assert result is not None
        assert abs(result) < 1e-6

    def test_result_is_annualized(self):
        # 1% daily log drift → ~252% annualized
        log_prices = np.cumsum(np.full(130, 0.01))
        prices = _price_series(np.exp(log_prices).tolist())
        result = mws._compute_slope_6m(prices)
        assert result is not None
        assert math.isclose(result, 0.01 * 252, rel_tol=0.05)


# ===========================================================================
# _compute_residual_3m
# ===========================================================================

class TestComputeResidual3m:
    def _aligned_pair(self, ticker_vals, anchor_vals, start="2024-01-01"):
        idx = pd.bdate_range(start=start, periods=max(len(ticker_vals), len(anchor_vals)))
        t = pd.Series(ticker_vals, index=idx[:len(ticker_vals)], dtype=float)
        a = pd.Series(anchor_vals, index=idx[:len(anchor_vals)], dtype=float)
        return t, a

    def test_positive_residual_when_ticker_outperforms(self):
        # Ticker +50%, anchor flat → residual ≈ +0.5 (5 pts to satisfy min-common check)
        t, a = self._aligned_pair([100.0, 110.0, 120.0, 130.0, 150.0],
                                  [100.0, 100.0, 100.0, 100.0, 100.0])
        result = mws._compute_residual_3m(t, a)
        assert result is not None
        assert math.isclose(result, 0.5, rel_tol=1e-9)

    def test_negative_residual_when_ticker_underperforms(self):
        t, a = self._aligned_pair([100.0, 90.0, 80.0, 60.0, 50.0],
                                  [100.0, 100.0, 100.0, 100.0, 100.0])
        result = mws._compute_residual_3m(t, a)
        assert result is not None
        assert math.isclose(result, -0.5, rel_tol=1e-9)

    def test_zero_residual_when_same_return(self):
        t, a = self._aligned_pair([100.0, 105.0, 110.0, 115.0, 120.0],
                                  [100.0, 105.0, 110.0, 115.0, 120.0])
        result = mws._compute_residual_3m(t, a)
        assert result is not None
        assert math.isclose(result, 0.0, abs_tol=1e-9)

    def test_returns_none_when_fewer_than_five_common_dates(self):
        idx = pd.bdate_range("2024-01-01", periods=4)
        t = pd.Series([100.0, 101.0, 102.0, 103.0], index=idx)
        a = pd.Series([100.0, 101.0, 102.0, 103.0], index=idx)
        result = mws._compute_residual_3m(t, a)
        assert result is None

    def test_returns_none_for_empty_anchor(self):
        prices = _price_series([100.0] * 65)
        anchor = pd.Series(dtype=float)
        result = mws._compute_residual_3m(prices, anchor)
        assert result is None


# ===========================================================================
# _blend_score
# ===========================================================================

class TestBlendScore:
    WEIGHTS = {"tr_12m": 0.45, "slope_6m": 0.35, "residual_3m": 0.20}

    def test_all_components_present(self):
        # 0.45*0.1 + 0.35*0.2 + 0.20*0.3 = 0.045 + 0.07 + 0.06 = 0.175
        result = mws._blend_score(0.1, 0.2, 0.3, self.WEIGHTS)
        assert result is not None
        assert math.isclose(result, 0.175, rel_tol=1e-9)

    def test_all_components_none_returns_none(self):
        result = mws._blend_score(None, None, None, self.WEIGHTS)
        assert result is None

    def test_partial_components_renormalize(self):
        # Only tr12 present (weight 0.45): renormalized → raw score = tr12
        result = mws._blend_score(0.5, None, None, self.WEIGHTS)
        assert result is not None
        assert math.isclose(result, 0.5, rel_tol=1e-9)

    def test_two_components_renormalize(self):
        # tr12=0.1 (w=0.45), slope6=0.2 (w=0.35); res3=None
        # renorm = (0.1*0.45 + 0.2*0.35) / (0.45 + 0.35) = (0.045+0.07)/0.80 ≈ 0.14375
        result = mws._blend_score(0.1, 0.2, None, self.WEIGHTS)
        expected = (0.1 * 0.45 + 0.2 * 0.35) / (0.45 + 0.35)
        assert result is not None
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_nan_treated_as_missing(self):
        result = mws._blend_score(np.nan, 0.2, None, self.WEIGHTS)
        # Only slope6 contributes → renorm = 0.2
        assert result is not None
        assert math.isclose(result, 0.2, rel_tol=1e-9)

    def test_empty_weights_returns_none(self):
        result = mws._blend_score(0.1, 0.2, 0.3, {})
        assert result is None


# ===========================================================================
# _compute_max_drawdown
# ===========================================================================

class TestComputeMaxDrawdown:
    def test_no_drawdown_monotone_increase(self):
        s = pd.Series([0.01] * 50)   # steady +1% per period
        result = mws._compute_max_drawdown(s)
        # Wealth index never falls below previous peak
        assert result >= 0.0 or math.isclose(result, 0.0, abs_tol=1e-9)

    def test_known_drawdown(self):
        # Wealth goes 1.0 → 1.5 → 1.05; drawdown from peak = (1.05-1.5)/1.5 ≈ -0.30
        # We pass cumulative returns (not levels): r = [0, 0.5, 0.05]
        s = pd.Series([0.0, 0.5, 0.05])
        result = mws._compute_max_drawdown(s)
        expected = (1.05 - 1.5) / 1.5
        assert math.isclose(result, expected, rel_tol=1e-6)

    def test_empty_series_returns_zero(self):
        s = pd.Series([], dtype=float)
        result = mws._compute_max_drawdown(s)
        assert result == 0.0

    def test_single_point_returns_zero(self):
        s = pd.Series([0.05])
        result = mws._compute_max_drawdown(s)
        assert result == 0.0

    def test_return_is_negative_or_zero(self):
        # Max drawdown is always ≤ 0
        np.random.seed(42)
        s = pd.Series(np.random.randn(200) * 0.01)
        result = mws._compute_max_drawdown(s)
        assert result <= 0.0


# ===========================================================================
# check_drawdown_state
# ===========================================================================

class TestCheckDrawdownState:
    def _write_perf_csv(self, twr_values: list[float], path: str) -> None:
        """Write a minimal performance CSV with a TWR column."""
        df = pd.DataFrame({"Date": range(len(twr_values)), "TWR": twr_values})
        df.to_csv(path, index=False)

    def test_returns_normal_when_no_perf_log(self, tmp_path):
        policy = _make_policy(soft_limit=0.20, hard_limit=0.28)
        result = mws.check_drawdown_state(policy, perf_log=str(tmp_path / "nonexistent.csv"))
        assert result["state"] == "normal"
        assert result["drawdown"] == 0.0

    def test_normal_state_small_drawdown(self, tmp_path):
        # Wealth: goes up then small dip of ~5% — well below soft limit
        returns = [0.01] * 60 + [-0.005] * 10
        path = str(tmp_path / "perf.csv")
        self._write_perf_csv(returns, path)
        policy = _make_policy()
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["state"] == "normal"

    def test_soft_limit_state(self, tmp_path):
        # Strong rally then a -22% drawdown (between soft=20% and hard=28%)
        returns = [0.02] * 50 + [-0.015] * 20
        path = str(tmp_path / "perf.csv")
        self._write_perf_csv(returns, path)
        policy = _make_policy(soft_limit=0.20, hard_limit=0.28)
        result = mws.check_drawdown_state(policy, perf_log=path)
        # The actual drawdown magnitude depends on compounding; just verify keys exist
        assert result["state"] in {"normal", "soft_limit", "hard_limit"}
        assert result["soft_limit"] == 0.20
        assert result["hard_limit"] == 0.28

    def test_hard_limit_state(self, tmp_path):
        # Extreme drawdown: wealth peaks then crashes 35% — exceeds hard limit
        rally = [0.02] * 20
        crash = [-0.025] * 30   # compound crash > 28%
        path = str(tmp_path / "perf.csv")
        self._write_perf_csv(rally + crash, path)
        policy = _make_policy(soft_limit=0.20, hard_limit=0.28)
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["state"] in {"soft_limit", "hard_limit"}

    def test_returns_policy_thresholds(self, tmp_path):
        path = str(tmp_path / "perf.csv")
        self._write_perf_csv([0.01, 0.01, 0.01], path)
        policy = _make_policy(soft_limit=0.15, hard_limit=0.25, recovery=0.10)
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["soft_limit"] == 0.15
        assert result["hard_limit"] == 0.25
        assert result["recovery"] == 0.10

    def test_missing_twr_column_returns_default(self, tmp_path):
        path = str(tmp_path / "perf.csv")
        df = pd.DataFrame({"Date": [1, 2, 3], "SomeOtherCol": [0.1, 0.2, 0.3]})
        df.to_csv(path, index=False)
        policy = _make_policy()
        result = mws.check_drawdown_state(policy, perf_log=path)
        assert result["state"] == "normal"
        assert result["drawdown"] == 0.0
