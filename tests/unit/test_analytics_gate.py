"""
tests/unit/test_analytics_gate.py

Tests for mws_analytics.check_execution_gate().

Regression bugs covered:
  Bug #9: Gate direction hardcoded to BUY — sell-defer and spike-trim were dead code.
  Bug #10: Spike-trim fired before trade direction was determined (checked too early).
"""
import os
import sys
import math

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import matplotlib; matplotlib.use("Agg")
import mws_analytics as mws


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_gate_policy(
    enabled=True,
    buy_sigma=2.0,
    sell_sigma=2.5,
    max_defer=10,
    vol_clamp=False,
    ewma_span=126,
):
    """Minimal policy dict with execution gate configured."""
    return {
        "execution_gates": {
            "short_term_confirmation": {
                "enabled":                  enabled,
                "buy_defer_sigma":          buy_sigma,
                "sell_defer_sigma":         sell_sigma,
                "max_defer_calendar_days":  max_defer,
                "ewma_span_days":           ewma_span,
                "vol_clamp_enabled":        vol_clamp,
            },
            "per_ticker_thresholds": {},
        }
    }


def _flat_prices_with_spike(n_base=200, spike_pct=0.12, direction="up"):
    """
    Build a price series with n_base stable rows + 1 spiked row.
    Stable rows: constant daily return 0.05% (low vol).
    Spike: large move on the last observation (index[-1]) relative to index[-3].

    To get a known z-score: z = ret_2d / vol_2d.
    With stable 0.05% daily returns and then a +12% spike over 2 days,
    the z-score will be large (>> 2.0).

    Returns (hist_df, ticker, expected_z_high)
    """
    idx = pd.bdate_range(end="2026-03-20", periods=n_base + 1)
    base_prices = 100.0 * np.exp(np.cumsum(np.full(n_base, 0.0005)))

    if direction == "up":
        spike_price = base_prices[-1] * (1 + spike_pct)
    else:
        spike_price = base_prices[-1] * (1 - spike_pct)

    prices_arr = np.append(base_prices, spike_price)
    s = pd.Series(prices_arr, index=idx)
    hist = pd.DataFrame({"VTI": s})
    hist.index.name = "Date"
    return hist, "VTI"


def _low_vol_prices(n=200):
    """
    Price series where the last 2 days are flat → ret_2d = 0 → z-score = 0 → proceed.

    The prior implementation used a constant drift which drove EWMA vol near zero,
    causing z = drift / near_zero → large → defer.  Making the last 2 returns exactly 0
    guarantees prices[-1] == prices[-3] and thus ret_2d = 0 regardless of vol level.
    """
    np.random.seed(42)
    rets = np.random.normal(0, 0.005, n)
    rets[-1] = 0.0   # last 2 days flat → prices[-1] == prices[-2] == prices[-3]
    rets[-2] = 0.0
    idx = pd.bdate_range(end="2026-03-20", periods=n)
    prices = 100.0 * np.exp(np.cumsum(rets))
    return pd.DataFrame({"VTI": pd.Series(prices, index=idx)})


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCheckExecutionGate:

    def test_gate_disabled_returns_proceed(self):
        """When gate.enabled = False, action is always 'proceed'."""
        policy = _make_gate_policy(enabled=False)
        hist = _low_vol_prices(200)
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        assert result["action"] == "proceed"
        assert result["reason"] == "gate_disabled"

    def test_no_price_history_returns_proceed(self):
        """When ticker is absent from hist, gate returns proceed (fail-open)."""
        policy = _make_gate_policy(enabled=True)
        hist   = pd.DataFrame({"OTHER": [100.0, 101.0] * 100})
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        assert result["action"] == "proceed"

    def test_buy_low_z_proceeds(self):
        """BUY with z << buy_sigma (2.0) → proceed."""
        policy = _make_gate_policy(enabled=True, buy_sigma=2.0)
        hist   = _low_vol_prices(200)
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        assert result["action"] == "proceed"

    @pytest.mark.regression
    def test_buy_high_z_defers(self):
        """
        Bug #9 regression: BUY with z >= buy_sigma → action == 'defer'.

        Previously the gate direction was hardcoded to BUY for all tickers,
        so this path worked. But this test also ensures the BUY branch is
        still correctly gating (confirm the fix didn't break BUY-defer).
        """
        policy = _make_gate_policy(enabled=True, buy_sigma=2.0)
        hist, ticker = _flat_prices_with_spike(n_base=200, spike_pct=0.12, direction="up")
        result = mws.check_execution_gate(policy, ticker, "BUY", hist)
        # z should be >> 2.0 due to the 12% spike
        assert result["action"] == "defer", (
            f"Expected 'defer' for large BUY z-score, got '{result['action']}' "
            f"(z={result.get('z_score')})"
        )

    @pytest.mark.regression
    def test_sell_high_z_spike_trim(self):
        """
        Bug #9 + Bug #10 regression: SELL with z >= +2.0 → action == 'spike_trim'.

        Bug #9: Previously direction was always "BUY", so the spike_trim branch
                (which requires direction == "SELL") could never fire.
        Bug #10: Spike-trim was checked before the direction was determined in
                 _action(), which could cause a BUY signal to wrongly spike-trim.
                 This test verifies spike_trim fires correctly for direction=SELL.
        """
        policy = _make_gate_policy(enabled=True, buy_sigma=2.0, sell_sigma=2.5)
        # Spike UP on a SELL direction → spike_trim (sell into strength)
        hist, ticker = _flat_prices_with_spike(n_base=200, spike_pct=0.12, direction="up")
        result = mws.check_execution_gate(policy, ticker, "SELL", hist)
        assert result["action"] == "spike_trim", (
            f"Expected 'spike_trim' for upward spike on SELL, got '{result['action']}'. "
            "Bug #9 or #10: direction may not be handled correctly."
        )

    @pytest.mark.regression
    def test_sell_low_z_defers(self):
        """
        Bug #9 regression: SELL with z <= -sell_sigma → action == 'defer'.

        Previously direction was hardcoded to BUY for all tickers, so
        sell-defer (z <= -2.5) was dead code and never executed.
        """
        policy = _make_gate_policy(enabled=True, sell_sigma=2.5)
        # Spike DOWN on SELL direction — large negative z
        hist, ticker = _flat_prices_with_spike(n_base=200, spike_pct=0.15, direction="down")
        result = mws.check_execution_gate(policy, ticker, "SELL", hist)
        assert result["action"] == "defer", (
            f"Expected 'defer' for sell with large negative z, got '{result['action']}'. "
            "Bug #9: sell-defer branch may be dead code (direction never set to SELL)."
        )

    def test_z_score_returned_in_result(self):
        """check_execution_gate always returns z_score when history is available."""
        policy = _make_gate_policy(enabled=True)
        hist   = _low_vol_prices(200)
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        assert "z_score" in result
        # z_score should be finite (may be None only for insufficient history)
        if result["z_score"] is not None:
            assert math.isfinite(result["z_score"])

    def test_per_ticker_sigma_override(self):
        """Per-ticker gate_sigma_buy override takes precedence over global."""
        policy = _make_gate_policy(enabled=True, buy_sigma=2.0)
        # Add a very high per-ticker override → gate won't fire even on a spike
        policy["execution_gates"]["per_ticker_thresholds"] = {
            "VTI": {"gate_sigma_buy": 99.0}
        }
        hist, ticker = _flat_prices_with_spike(n_base=200, spike_pct=0.12, direction="up")
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        # With 99σ threshold, even a 12% spike won't trigger defer
        assert result["action"] == "proceed", (
            f"Per-ticker override (99σ) should prevent defer, got '{result['action']}'"
        )

    def test_insufficient_history_returns_proceed(self):
        """Fewer than span//2 observations → returns proceed (insufficient vol history)."""
        policy = _make_gate_policy(enabled=True, ewma_span=126)
        # Only 10 rows — far below span//2=63
        idx = pd.bdate_range(end="2026-03-20", periods=10)
        s   = pd.Series([100.0] * 10, index=idx)
        hist = pd.DataFrame({"VTI": s})
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        assert result["action"] == "proceed"
