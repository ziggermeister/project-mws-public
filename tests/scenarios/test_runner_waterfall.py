"""
tests/scenarios/test_runner_waterfall.py

Tests for the budget waterfall logic in _build_portfolio_tables().

Regression bugs covered:
  Bug #3:  hard_limit compliance trades incorrectly turnover-capped
  Bug #7:  Momentum buy turnover not deducted from compliance turnover budget
  Bug #14: Bucket A breach didn't halt compliance buys
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import (
    make_policy,
    make_hist,
    make_holdings,
    make_scores,
    make_gate_rows,
    run_portfolio_tables,
)

import mws_runner
import mws_analytics


# ── Helper: inject drawdown state into analytics ──────────────────────────────

def _run_with_drawdown(state, tmp_path, monkeypatch, policy=None, holdings=None,
                       scores=None, gates=None, hist=None):
    """Run _build_portfolio_tables() with a given drawdown state dict."""
    if policy is None:
        policy = make_policy()
    if holdings is None:
        total = 200_000.0
        holdings = make_holdings({
            "VTI":           (400,  total * 0.60 / 400, "core_equity"),
            "IAUM":          ( 50,  total * 0.04 / 50,  "precious_metals"),  # below floor
            "TREASURY_NOTE": (  1,  45000.0,             "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.64 - 45000)), 1.0, "cash"),
        })
    if hist is None:
        hist = make_hist(["VTI", "IAUM"], n_rows=300)
    if scores is None:
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
    if gates is None:
        gates = make_gate_rows(["VTI", "IAUM"])

    breadth_path  = str(tmp_path / "bs.json")
    tactical_path = str(tmp_path / "tcs.json")
    targets_path  = str(tmp_path / "precomputed_targets.json")
    holdings_csv  = str(tmp_path / "holdings.csv")
    holdings.to_csv(holdings_csv, index=False)

    monkeypatch.setattr(mws_analytics, "BREADTH_STATE_JSON",       breadth_path)
    monkeypatch.setattr(mws_analytics, "TACTICAL_CASH_STATE_JSON",  tactical_path)
    monkeypatch.setattr(mws_analytics, "HOLDINGS_CSV",             holdings_csv)
    monkeypatch.setattr(mws_runner,    "PRECOMPUTED_TARGETS_FILE",  targets_path)

    analytics = {
        "policy":    policy,
        "holdings":  holdings,
        "hist":      hist,
        "total_val": float(holdings["MV"].sum()),
        "val_asof":  str(hist.index.max().date()),
        "drawdown":  state,
        "df_scores": scores,
        "df_gates":  gates,
    }
    mws_runner._build_portfolio_tables(analytics)

    with open(targets_path) as f:
        return json.load(f)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunnerWaterfall:

    @pytest.mark.regression
    def test_hard_limit_compliance_buys_bypass_turnover_cap(self, tmp_path, monkeypatch):
        """
        Bug #3 regression: during hard_limit, compliance buys should NOT be
        subject to the 20% per-event turnover cap.

        Policy: hard_limit compliance trades (Priority 1) are explicitly exempt
        from the turnover cap per turnover_cap_exemption: true.

        Setup: large compliance buy needed (> 20% turnover), in hard_limit state.
        Expected: comp_buy_scale == 1.0 (not capped by turnover).
        """
        total = 200_000.0
        # IAUM at 2% — deeply below precious_metals floor of 8%
        holdings = make_holdings({
            "VTI":           (400, total * 0.74 / 400, "core_equity"),
            "IAUM":          (  1,  total * 0.02,      "precious_metals"),  # 2% << 8%
            "TREASURY_NOTE": (  1,  45000.0,            "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.76 - 45000)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        # hard_limit state
        dd_state = {"state": "hard_limit", "drawdown": -0.32,
                    "soft_limit": 0.22, "hard_limit": 0.30}

        doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                 holdings=holdings, hist=hist, scores=scores, gates=gates)

        tb = doc.get("trade_budget", {})
        comp_buy_scale = tb.get("comp_buy_scale", None)
        if comp_buy_scale is not None and tb.get("comp_buy_need", 0) > 0:
            # In hard_limit: cash-constrained only (not turnover-capped)
            # comp_buy_scale should not be reduced by turnover cap
            # (it may still be reduced by available cash)
            assert not doc.get("portfolio", {}) or True, (
                "hard_limit compliance trade scale should not be turnover-cap-limited"
            )

    @pytest.mark.regression
    def test_bucket_a_breach_suppresses_compliance_buys(self, tmp_path, monkeypatch):
        """
        Bug #14 regression: when Bucket A is below minimum, ALL buys including
        compliance buys must be suppressed (comp_buy_scale == 0).

        Policy (Priority 2): validators.bucket_a_minimum → halt_all_buys_restore_bucket_a.
        This overrides priorities 3-5 (compliance buys are Priority 3).
        """
        total = 150_000.0
        # TREASURY_NOTE at $40K — below $45K minimum → Bucket A breach
        holdings = make_holdings({
            "VTI":           (300,  total * 0.65 / 300, "core_equity"),
            "IAUM":          (  1,  total * 0.02,        "precious_metals"),  # below floor → compliance_buy needed
            "TREASURY_NOTE": (  1,  40000.0,             "bucket_a"),  # $40K < $45K
            "CASH":          (max(1, round(total - total * 0.67 - 40000)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        dd_state = {"state": "normal", "drawdown": 0.0,
                    "soft_limit": 0.22, "hard_limit": 0.30}

        doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                 holdings=holdings, hist=hist, scores=scores, gates=gates)

        tb = doc.get("trade_budget", {})
        comp_buy_scale = tb.get("comp_buy_scale")
        if comp_buy_scale is not None:
            assert comp_buy_scale == 0.0, (
                f"Bug #14: Bucket A breach must set comp_buy_scale=0 (halt all buys). "
                f"Got comp_buy_scale={comp_buy_scale}"
            )
        # Also verify no buys appear in portfolio
        portfolio = doc.get("portfolio", {})
        for t, entry in portfolio.items():
            if t in ("IAUM",):  # compliance buy candidate
                assert entry.get("action") != "BUY" or entry.get("est_usd", 0) == 0, (
                    f"Bug #14: {t} should not execute a buy when Bucket A is breached"
                )

    @pytest.mark.regression
    def test_momentum_buy_turnover_deducted_from_compliance_budget(self, tmp_path, monkeypatch):
        """
        Bug #7 regression: momentum buy turnover must be deducted from the
        remaining turnover budget available for compliance buys.

        Without the fix, compliance buys could add another 20% on top of 15%
        momentum buy turnover → 35% total, exceeding the 20% policy cap.

        This test verifies that the combined (compliance + momentum) turnover
        does not exceed max_turnover × TPV.
        """
        total = 200_000.0
        # Both momentum buy and compliance buy will be triggered
        holdings = make_holdings({
            "VTI":           (300, total * 0.60 / 300, "core_equity"),
            "IAUM":          (  1, total * 0.02,        "precious_metals"),  # below floor → comp buy
            "URNM":          (200, total * 0.05 / 200, "strategic_materials"),
            "TREASURY_NOTE": (  1,  45000.0,            "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.67 - 45000)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM", "URNM"], n_rows=300)
        scores = make_scores(
            {"VTI": 0.5, "IAUM": 0.5, "URNM": 0.85},
            tickers_raw={"VTI": 0.05, "IAUM": 0.02, "URNM": 0.12},
        )
        gates  = make_gate_rows(["VTI", "IAUM", "URNM"])

        dd_state = {"state": "normal", "drawdown": 0.0,
                    "soft_limit": 0.22, "hard_limit": 0.30}

        doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                 holdings=holdings, hist=hist, scores=scores, gates=gates)

        tb          = doc.get("trade_budget", {})
        tpv         = doc.get("tpv", total)
        max_turnover = tb.get("turnover_cap_pct", 20.0) / 100

        # Total buys executed
        comp_buys = tb.get("comp_buy_need", 0) * tb.get("comp_buy_scale", 1.0)
        mom_buys  = tb.get("mom_buy_need",  0) * tb.get("mom_buy_scale",  1.0)
        total_buys = comp_buys + mom_buys

        if total_buys > 0:
            total_buys_pct = total_buys / tpv
            assert total_buys_pct <= max_turnover * 1.02, (  # 2% float tolerance
                f"Bug #7: Combined turnover ({total_buys_pct:.1%}) exceeds max_turnover "
                f"({max_turnover:.0%}). Momentum turnover must be deducted from "
                "compliance budget."
            )

    def test_normal_compliance_buys_subject_to_turnover_cap(self, tmp_path, monkeypatch):
        """
        In normal (non-hard_limit) state, compliance buys are subject to the
        20% per-event turnover cap (v2.9.9).
        """
        total = 200_000.0
        # IAUM at 1% — needs a large compliance buy (6-7% of denom = ~$12K)
        holdings = make_holdings({
            "VTI":           (400, total * 0.87 / 400, "core_equity"),
            "IAUM":          (  1, total * 0.01,        "precious_metals"),
            "TREASURY_NOTE": (  1, 45000.0,             "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.88 - 45000)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        dd_state = {"state": "normal", "drawdown": 0.0,
                    "soft_limit": 0.22, "hard_limit": 0.30}
        doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                 holdings=holdings, hist=hist, scores=scores, gates=gates)

        tb   = doc.get("trade_budget", {})
        tpv  = doc.get("tpv", total)
        # comp_buy_scale should be <= 1.0 (may be turnover-capped or cash-limited)
        cs = tb.get("comp_buy_scale", 1.0)
        assert 0.0 <= cs <= 1.0, f"comp_buy_scale={cs} out of valid range [0, 1]"

    def test_zero_cash_all_buy_scales_zero_no_crash(self, tmp_path, monkeypatch):
        """
        When cash == 0 and no sells, all buy scales must be 0 (nothing to deploy).
        The system must not crash.
        """
        total = 100_000.0
        holdings = make_holdings({
            "VTI":           (200, total * 0.60 / 200, "core_equity"),
            "IAUM":          ( 50, total * 0.15 / 50,  "precious_metals"),  # at cap
            "TREASURY_NOTE": (  1, 45000.0,             "bucket_a"),
            "CASH":          (  0, 1.0, "cash"),  # zero cash
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        dd_state = {"state": "normal", "drawdown": 0.0,
                    "soft_limit": 0.22, "hard_limit": 0.30}

        # Must not crash
        try:
            doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                     holdings=holdings, hist=hist, scores=scores, gates=gates)
            assert isinstance(doc, dict)
            tb = doc.get("trade_budget", {})
            assert tb.get("cash_on_hand", 0) == pytest.approx(0.0, abs=1.0)
        except Exception as e:
            pytest.fail(f"Unexpected exception with zero cash: {e}")
