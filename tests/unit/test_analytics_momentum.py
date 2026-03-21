"""
tests/unit/test_analytics_momentum.py

Tests for mws_analytics.generate_rankings() and run_mws_audit().

Regression bugs covered:
  Bug #16: Activated tickers were included in momentum ranking universe
           (they are eligible_for_momentum: false in policy).
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

def _make_price_hist(tickers, n=300, seed=42):
    """Wide-format price history DataFrame for the given tickers."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-03-20", periods=n)
    data = {}
    for t in tickers:
        lr = rng.normal(0.0003, 0.012, n)
        p  = 100.0 * np.exp(np.cumsum(lr))
        p[0] = 100.0
        data[t] = p
    df = pd.DataFrame(data, index=idx)
    df.index.name = "Date"
    return df


def _minimal_policy(inducted=None, activated=None, reference=None, extra_tickers=None):
    """
    Build a policy dict with the given tickers set to the specified lifecycle stages.

    inducted  : list of ticker strings with stage="inducted"
    activated : list of ticker strings with stage="activated"
    reference : list of ticker strings with stage="reference"
    """
    inducted  = inducted  or []
    activated = activated or []
    reference = reference or []

    tc = {}
    for t in inducted:
        tc[t] = {"lifecycle": {"stage": "inducted"}}
    for t in activated:
        tc[t] = {"lifecycle": {"stage": "activated"}}
    for t in reference:
        tc[t] = {"lifecycle": {"stage": "reference"}}
    if extra_tickers:
        for t, cfg in extra_tickers.items():
            tc[t] = cfg

    return {
        "ticker_constraints": tc,
        "ticker_to_sleeves":  {t: {"core_equity": 1.0} for t in inducted + activated + reference},
        "governance": {
            "reporting_baselines": {
                "active_benchmarks":  ["SPY"],
                "corr_anchor_ticker": "VTI",
                "alpha_start_date":   "2024-01-01",
            },
            "fixed_asset_prices": {"CASH": 1.0},
        },
        "momentum_engine": {
            "signal_weights": {"tr_12m": 0.45, "slope_6m": 0.35, "residual_3m": 0.20},
        },
        "corr_anchor_ticker": "VTI",
    }


def _holdings_df(tickers):
    """Minimal holdings DataFrame with positive shares for each ticker."""
    rows = [{"Ticker": t, "Shares": 100, "Class": "core_equity"} for t in tickers]
    return pd.DataFrame(rows)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunMwsAudit:

    @pytest.mark.regression
    def test_activated_tickers_excluded_from_ranking_universe(self):
        """
        Bug #16 regression: activated tickers must NOT appear in the candidates list.

        Policy says activated tickers have eligible_for_momentum: false.
        Including them distorts percentile ranks for valid allocated instruments.
        """
        all_tickers = ["VTI", "SOXQ", "NEW_ACTIVATED"]
        hist = _make_price_hist(all_tickers, n=300)
        policy = _minimal_policy(
            inducted=["VTI", "SOXQ"],
            activated=["NEW_ACTIVATED"],
        )
        hold = _holdings_df(["VTI", "SOXQ"])  # activated ticker not held

        candidates, _, missing = mws.run_mws_audit(policy, hist, hold)

        assert "NEW_ACTIVATED" not in candidates, (
            "Bug #16: activated ticker must be excluded from ranking universe. "
            "Including it distorts percentile ranks for inducted tickers."
        )
        assert "VTI"  in candidates
        assert "SOXQ" in candidates

    def test_held_tickers_always_included_in_candidates(self):
        """Held tickers (positive shares) are always in candidates for reporting continuity."""
        all_tickers = ["VTI", "SOXQ"]
        hist = _make_price_hist(all_tickers, n=300)
        policy = _minimal_policy(inducted=["VTI", "SOXQ"])
        hold = _holdings_df(["VTI", "SOXQ"])

        candidates, _, _ = mws.run_mws_audit(policy, hist, hold)
        assert "VTI"  in candidates
        assert "SOXQ" in candidates

    def test_reference_tickers_excluded_from_candidates(self):
        """Reference tickers (benchmarks) are NOT included in ranking candidates."""
        all_tickers = ["VTI", "SPY"]
        hist = _make_price_hist(all_tickers, n=300)
        policy = _minimal_policy(inducted=["VTI"], reference=["SPY"])
        hold = _holdings_df(["VTI"])

        candidates, _, _ = mws.run_mws_audit(policy, hist, hold)
        assert "SPY" not in candidates
        assert "VTI" in candidates


class TestGenerateRankings:

    def test_all_percentile_ranks_in_0_1(self):
        """All Pct values must be in [0.0, 1.0] — this is a fundamental invariant."""
        tickers = ["VTI", "IAUM", "URNM", "IBIT", "XBI"]
        hist    = _make_price_hist(tickers, n=300)
        policy  = _minimal_policy(inducted=tickers)
        hold    = _holdings_df(tickers)
        candidates, _, _ = mws.run_mws_audit(policy, hist, hold)
        df = mws.generate_rankings(policy, hist, candidates, hold)

        if not df.empty:
            assert df["Pct"].between(0.0, 1.0, inclusive="both").all(), (
                f"Percentile ranks out of [0,1]: {df[['Ticker','Pct']]}"
            )

    def test_returns_correct_columns(self):
        """Output DataFrame has required columns."""
        tickers = ["VTI", "IAUM"]
        hist    = _make_price_hist(tickers, n=300)
        policy  = _minimal_policy(inducted=tickers)
        hold    = _holdings_df(tickers)
        candidates, _, _ = mws.run_mws_audit(policy, hist, hold)
        df = mws.generate_rankings(policy, hist, candidates, hold)

        for col in ["Ticker", "Score", "Pct", "RawScore", "Alpha", "AlphaVs", "Sleeve", "Status"]:
            assert col in df.columns, f"Missing column: {col}"

    def test_empty_candidates_returns_empty_df(self):
        """Empty candidates list → empty DataFrame with correct columns."""
        hist   = _make_price_hist(["VTI"], n=300)
        policy = _minimal_policy(inducted=["VTI"])
        hold   = _holdings_df([])
        df = mws.generate_rankings(policy, hist, [], hold)

        assert df.empty
        for col in ["Ticker", "Score", "Pct", "RawScore", "Alpha"]:
            assert col in df.columns

    def test_single_ticker_gets_pct_1(self):
        """A single ranked ticker gets Pct == 1.0 (100th percentile)."""
        tickers = ["VTI"]
        hist    = _make_price_hist(tickers, n=300)
        policy  = _minimal_policy(inducted=tickers)
        hold    = _holdings_df(tickers)
        candidates, _, _ = mws.run_mws_audit(policy, hist, hold)
        df = mws.generate_rankings(policy, hist, candidates, hold)

        if not df.empty:
            assert len(df) == 1
            # Single ticker: pd.Series.rank(pct=True) returns 1.0 for the only element
            assert df.iloc[0]["Pct"] == pytest.approx(1.0)

    @pytest.mark.regression
    def test_activated_ticker_not_in_rankings(self):
        """
        Bug #16 regression: activated ticker does not appear in generate_rankings() output.

        run_mws_audit() already excludes activated from candidates; confirm
        generate_rankings() doesn't independently add them back.
        """
        all_tickers = ["VTI", "SOXQ", "ACTIVATED_TEST"]
        hist = _make_price_hist(all_tickers, n=300)
        policy = _minimal_policy(inducted=["VTI", "SOXQ"], activated=["ACTIVATED_TEST"])
        hold = _holdings_df(["VTI", "SOXQ"])

        candidates, _, _ = mws.run_mws_audit(policy, hist, hold)
        df = mws.generate_rankings(policy, hist, candidates, hold)

        assert "ACTIVATED_TEST" not in df["Ticker"].values, (
            "Bug #16: activated ticker should not appear in rankings output."
        )
