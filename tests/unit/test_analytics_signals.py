"""
tests/unit/test_analytics_signals.py

Unit tests for momentum signal computation functions in mws_analytics:
  - get_held_tickers
  - _compute_tr12m   (12-month total return)
  - _compute_slope_6m (annualised OLS log-price slope)
  - _compute_residual_3m (ticker vs anchor residual)
  - _blend_score     (weighted blend, signal weights from policy)
  - _compute_max_drawdown (peak-to-trough utility)

Signal-weight assertions use make_policy() so they stay in sync with
the canonical policy fixture rather than duplicating literal constants.

Migrated from test_mws.py (root-level file excluded from testpaths).
"""
import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import matplotlib; matplotlib.use("Agg")
import mws_analytics as mws
from tests.conftest import make_policy


# ── Helpers ───────────────────────────────────────────────────────────────────

def _price_series(values, start="2024-01-01") -> pd.Series:
    """Build a pd.Series of prices indexed by business-day DatetimeIndex."""
    idx = pd.bdate_range(start=start, periods=len(values))
    return pd.Series(values, index=idx, dtype=float)


def _make_hold(rows: list) -> pd.DataFrame:
    """Build a minimal holdings DataFrame with Ticker and Shares columns."""
    return pd.DataFrame(rows, columns=["Ticker", "Shares"])


# ── get_held_tickers ──────────────────────────────────────────────────────────

class TestGetHeldTickers:

    def test_returns_positive_share_tickers(self):
        hold = _make_hold([
            {"Ticker": "VTI",  "Shares": "100"},
            {"Ticker": "SOXQ", "Shares": "50"},
        ])
        assert mws.get_held_tickers(hold) == {"VTI", "SOXQ"}

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
        assert mws.get_held_tickers(hold) == set()

    def test_missing_columns_returns_empty_set(self):
        hold = pd.DataFrame({"Symbol": ["VTI"], "Qty": [100]})
        assert mws.get_held_tickers(hold) == set()

    def test_normalizes_ticker_case(self):
        hold = _make_hold([{"Ticker": "vti", "Shares": "100"}])
        assert "VTI" in mws.get_held_tickers(hold)

    def test_strips_whitespace(self):
        hold = _make_hold([{"Ticker": "  VTI  ", "Shares": " 100 "}])
        assert "VTI" in mws.get_held_tickers(hold)


# ── _compute_tr12m ────────────────────────────────────────────────────────────

class TestComputeTr12m:

    def test_positive_return(self):
        prices = _price_series([100.0] * 126 + [200.0])
        result = mws._compute_tr12m(prices)
        assert result is not None
        assert math.isclose(result, 1.0, rel_tol=1e-9)

    def test_negative_return(self):
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
        assert mws._compute_tr12m(_price_series([100.0])) is None

    def test_empty_returns_none(self):
        assert mws._compute_tr12m(_price_series([])) is None

    def test_ignores_nan_values(self):
        prices = pd.Series([100.0, np.nan, np.nan, 150.0])
        result = mws._compute_tr12m(prices)
        assert result is not None
        assert math.isclose(result, 0.5, rel_tol=1e-9)


# ── _compute_slope_6m ─────────────────────────────────────────────────────────

class TestComputeSlope6m:

    def test_upward_trend_positive_slope(self):
        prices = _price_series(np.linspace(100, 200, 130).tolist())
        result = mws._compute_slope_6m(prices)
        assert result is not None
        assert result > 0

    def test_downward_trend_negative_slope(self):
        prices = _price_series(np.linspace(200, 100, 130).tolist())
        result = mws._compute_slope_6m(prices)
        assert result is not None
        assert result < 0

    def test_too_few_points_returns_none(self):
        assert mws._compute_slope_6m(_price_series([100.0] * 9)) is None

    def test_flat_prices_near_zero_slope(self):
        prices = _price_series([100.0] * 130)
        result = mws._compute_slope_6m(prices)
        assert result is not None
        assert abs(result) < 1e-6

    def test_result_is_annualized(self):
        log_prices = np.cumsum(np.full(130, 0.01))
        prices = _price_series(np.exp(log_prices).tolist())
        result = mws._compute_slope_6m(prices)
        assert result is not None
        assert math.isclose(result, 0.01 * 252, rel_tol=0.05)


# ── _compute_residual_3m ──────────────────────────────────────────────────────

class TestComputeResidual3m:

    def _aligned_pair(self, ticker_vals, anchor_vals):
        idx = pd.bdate_range("2024-01-01", periods=max(len(ticker_vals), len(anchor_vals)))
        t = pd.Series(ticker_vals, index=idx[:len(ticker_vals)], dtype=float)
        a = pd.Series(anchor_vals, index=idx[:len(anchor_vals)], dtype=float)
        return t, a

    def test_positive_residual_when_ticker_outperforms(self):
        t, a = self._aligned_pair(
            [100.0, 110.0, 120.0, 130.0, 150.0],
            [100.0, 100.0, 100.0, 100.0, 100.0],
        )
        result = mws._compute_residual_3m(t, a)
        assert result is not None
        assert math.isclose(result, 0.5, rel_tol=1e-9)

    def test_negative_residual_when_ticker_underperforms(self):
        t, a = self._aligned_pair(
            [100.0, 90.0, 80.0, 60.0, 50.0],
            [100.0, 100.0, 100.0, 100.0, 100.0],
        )
        result = mws._compute_residual_3m(t, a)
        assert result is not None
        assert math.isclose(result, -0.5, rel_tol=1e-9)

    def test_zero_residual_when_same_return(self):
        t, a = self._aligned_pair(
            [100.0, 105.0, 110.0, 115.0, 120.0],
            [100.0, 105.0, 110.0, 115.0, 120.0],
        )
        result = mws._compute_residual_3m(t, a)
        assert result is not None
        assert math.isclose(result, 0.0, abs_tol=1e-9)

    def test_returns_none_fewer_than_five_common_dates(self):
        idx = pd.bdate_range("2024-01-01", periods=4)
        t = pd.Series([100.0, 101.0, 102.0, 103.0], index=idx)
        a = pd.Series([100.0, 101.0, 102.0, 103.0], index=idx)
        assert mws._compute_residual_3m(t, a) is None

    def test_returns_none_for_empty_anchor(self):
        prices = _price_series([100.0] * 65)
        anchor = pd.Series(dtype=float)
        assert mws._compute_residual_3m(prices, anchor) is None


# ── _blend_score ──────────────────────────────────────────────────────────────

class TestBlendScore:
    """Signal weights are read from make_policy() so they stay in sync."""

    @classmethod
    def setup_class(cls):
        cls.WEIGHTS = make_policy()["momentum_engine"]["signal_weights"]

    def test_all_components_present(self):
        # 0.45*0.1 + 0.35*0.2 + 0.20*0.3 = 0.045 + 0.07 + 0.06 = 0.175
        w = self.WEIGHTS
        expected = (
            w["tr_12m"] * 0.1
            + w["slope_6m"] * 0.2
            + w["residual_3m"] * 0.3
        )
        result = mws._blend_score(0.1, 0.2, 0.3, w)
        assert result is not None
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_all_components_none_returns_none(self):
        assert mws._blend_score(None, None, None, self.WEIGHTS) is None

    def test_partial_components_renormalize(self):
        # Only tr12 present — renormalized raw score equals tr12
        result = mws._blend_score(0.5, None, None, self.WEIGHTS)
        assert result is not None
        assert math.isclose(result, 0.5, rel_tol=1e-9)

    def test_two_components_renormalize(self):
        w = self.WEIGHTS
        expected = (0.1 * w["tr_12m"] + 0.2 * w["slope_6m"]) / (
            w["tr_12m"] + w["slope_6m"]
        )
        result = mws._blend_score(0.1, 0.2, None, w)
        assert result is not None
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_nan_treated_as_missing(self):
        w = self.WEIGHTS
        # Only slope6 contributes → renorm = 0.2
        result = mws._blend_score(np.nan, 0.2, None, w)
        assert result is not None
        assert math.isclose(result, 0.2, rel_tol=1e-9)

    def test_empty_weights_returns_none(self):
        assert mws._blend_score(0.1, 0.2, 0.3, {}) is None


# ── _compute_max_drawdown ─────────────────────────────────────────────────────

class TestComputeMaxDrawdown:

    def test_no_drawdown_monotone_increase(self):
        s = pd.Series([0.01] * 50)
        result = mws._compute_max_drawdown(s)
        assert result >= 0.0 or math.isclose(result, 0.0, abs_tol=1e-9)

    def test_known_drawdown(self):
        # Wealth: 1.0 → 1.5 → 1.05; drawdown = (1.05-1.5)/1.5 ≈ -0.30
        # Pass cumulative returns (not levels): [0, 0.5, 0.05]
        s = pd.Series([0.0, 0.5, 0.05])
        result = mws._compute_max_drawdown(s)
        expected = (1.05 - 1.5) / 1.5
        assert math.isclose(result, expected, rel_tol=1e-6)

    def test_empty_series_returns_zero(self):
        assert mws._compute_max_drawdown(pd.Series([], dtype=float)) == 0.0

    def test_single_point_returns_zero(self):
        assert mws._compute_max_drawdown(pd.Series([0.05])) == 0.0

    def test_return_is_negative_or_zero(self):
        rng = np.random.default_rng(42)
        s = pd.Series(rng.normal(0, 0.01, 200))
        assert mws._compute_max_drawdown(s) <= 0.0
