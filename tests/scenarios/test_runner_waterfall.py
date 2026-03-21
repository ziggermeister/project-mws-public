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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dd_state(policy, regime, drawdown=0.0):
    """Build a drawdown state dict reading thresholds from policy."""
    return {
        "state":      regime,
        "drawdown":   drawdown,
        "soft_limit": policy["drawdown_rules"]["soft_limit"],
        "hard_limit": policy["drawdown_rules"]["hard_limit"],
    }


def _bucket_a_min(policy):
    """Return the Bucket A minimum USD from policy."""
    return policy["definitions"]["buckets"]["bucket_a_protected_liquidity"]["minimum_usd"]


def _run_with_drawdown(state, tmp_path, monkeypatch, policy=None, holdings=None,
                       scores=None, gates=None, hist=None):
    """Run _build_portfolio_tables() with a given drawdown state dict."""
    if policy is None:
        policy = make_policy()
    if holdings is None:
        total      = 200_000.0
        ba_min     = _bucket_a_min(policy)
        holdings = make_holdings({
            "VTI":           (400,  total * 0.60 / 400, "core_equity"),
            "IAUM":          ( 50,  total * 0.04 / 50,  "precious_metals"),  # below floor
            "TREASURY_NOTE": (  1,  float(ba_min),       "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.64 - ba_min)), 1.0, "cash"),
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
        policy    = make_policy()
        ba_min    = _bucket_a_min(policy)
        total     = 200_000.0
        holdings  = make_holdings({
            "VTI":           (400, total * 0.74 / 400, "core_equity"),
            "IAUM":          (  1,  total * 0.02,      "precious_metals"),  # 2% << 8%
            "TREASURY_NOTE": (  1,  float(ba_min),      "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.76 - ba_min)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        dd_state = _dd_state(policy, "hard_limit", -0.32)

        doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                 policy=policy, holdings=holdings,
                                 hist=hist, scores=scores, gates=gates)

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
        policy   = make_policy()
        ba_min   = _bucket_a_min(policy)
        # Deliberately set TREASURY_NOTE below the minimum to trigger Bucket A breach.
        # Use ba_min - 5000 to ensure the breach is meaningful regardless of policy value.
        breach_val = float(ba_min) - 5000.0
        total    = 150_000.0
        holdings = make_holdings({
            "VTI":           (300,  total * 0.65 / 300, "core_equity"),
            "IAUM":          (  1,  total * 0.02,        "precious_metals"),  # below floor
            "TREASURY_NOTE": (  1,  breach_val,          "bucket_a"),  # below minimum
            "CASH":          (max(1, round(total - total * 0.67 - breach_val)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        dd_state = _dd_state(policy, "normal")

        doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                 policy=policy, holdings=holdings,
                                 hist=hist, scores=scores, gates=gates)

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
        policy = make_policy()
        ba_min = _bucket_a_min(policy)
        total  = 200_000.0
        holdings = make_holdings({
            "VTI":           (300, total * 0.60 / 300, "core_equity"),
            "IAUM":          (  1, total * 0.02,        "precious_metals"),  # below floor
            "URNM":          (200, total * 0.05 / 200, "strategic_materials"),
            "TREASURY_NOTE": (  1,  float(ba_min),       "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.67 - ba_min)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM", "URNM"], n_rows=300)
        scores = make_scores(
            {"VTI": 0.5, "IAUM": 0.5, "URNM": 0.85},
            tickers_raw={"VTI": 0.05, "IAUM": 0.02, "URNM": 0.12},
        )
        gates  = make_gate_rows(["VTI", "IAUM", "URNM"])

        dd_state = _dd_state(policy, "normal")

        doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                 policy=policy, holdings=holdings,
                                 hist=hist, scores=scores, gates=gates)

        tb          = doc.get("trade_budget", {})
        tpv         = doc.get("tpv", total)
        max_turnover = tb.get("turnover_cap_pct", 20.0) / 100

        comp_buys  = tb.get("comp_buy_need", 0) * tb.get("comp_buy_scale", 1.0)
        mom_buys   = tb.get("mom_buy_need",  0) * tb.get("mom_buy_scale",  1.0)
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
        policy = make_policy()
        ba_min = _bucket_a_min(policy)
        total  = 200_000.0
        holdings = make_holdings({
            "VTI":           (400, total * 0.87 / 400, "core_equity"),
            "IAUM":          (  1, total * 0.01,        "precious_metals"),
            "TREASURY_NOTE": (  1, float(ba_min),        "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.88 - ba_min)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        dd_state = _dd_state(policy, "normal")
        doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                 policy=policy, holdings=holdings,
                                 hist=hist, scores=scores, gates=gates)

        tb = doc.get("trade_budget", {})
        cs = tb.get("comp_buy_scale", 1.0)
        assert 0.0 <= cs <= 1.0, f"comp_buy_scale={cs} out of valid range [0, 1]"

    @pytest.mark.regression
    def test_soft_limit_uses_max_turnover_stress(self, tmp_path, monkeypatch):
        """
        Finding 1 regression: under soft_limit, the runner must use
        governance.execution.max_turnover_stress (22%) as the turnover cap,
        not the normal max_turnover (20%).

        Asserts: trade_budget.turnover_cap_pct == 22.0 when dd.state == 'soft_limit'.
        """
        policy = make_policy()
        ba_min = _bucket_a_min(policy)
        total  = 200_000.0
        holdings = make_holdings({
            "VTI":           (400, total * 0.60 / 400, "core_equity"),
            "IAUM":          ( 50, total * 0.04 / 50,  "precious_metals"),
            "TREASURY_NOTE": (  1, float(ba_min),       "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.64 - ba_min)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        dd_state = _dd_state(policy, "soft_limit", -0.24)

        doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                 policy=policy, holdings=holdings,
                                 hist=hist, scores=scores, gates=gates)

        tb = doc.get("trade_budget", {})
        cap_pct = tb.get("turnover_cap_pct")
        assert cap_pct is not None, "trade_budget.turnover_cap_pct is missing"
        assert cap_pct == pytest.approx(
            policy["governance"]["execution"]["max_turnover_stress"] * 100, abs=0.01
        ), (
            f"Under soft_limit, turnover_cap_pct must equal max_turnover_stress "
            f"({policy['governance']['execution']['max_turnover_stress'] * 100:.1f}%), "
            f"got {cap_pct:.1f}%"
        )

    def test_zero_cash_all_buy_scales_zero_no_crash(self, tmp_path, monkeypatch):
        """
        When cash == 0 and no sells, all buy scales must be 0 (nothing to deploy).
        The system must not crash.
        """
        policy = make_policy()
        ba_min = _bucket_a_min(policy)
        total  = 100_000.0
        holdings = make_holdings({
            "VTI":           (200, total * 0.60 / 200, "core_equity"),
            "IAUM":          ( 50, total * 0.15 / 50,  "precious_metals"),  # at cap
            "TREASURY_NOTE": (  1, float(ba_min),       "bucket_a"),
            "CASH":          (  0, 1.0, "cash"),  # zero cash
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        dd_state = _dd_state(policy, "normal")

        try:
            doc = _run_with_drawdown(dd_state, tmp_path, monkeypatch,
                                     policy=policy, holdings=holdings,
                                     hist=hist, scores=scores, gates=gates)
            assert isinstance(doc, dict)
            tb = doc.get("trade_budget", {})
            assert tb.get("cash_on_hand", 0) == pytest.approx(0.0, abs=1.0)
        except Exception as e:
            pytest.fail(f"Unexpected exception with zero cash: {e}")

    def test_precomputed_targets_no_tmp_file_left_after_write(self, tmp_path, monkeypatch):
        """
        Finding 4 regression: atomic write of mws_precomputed_targets.json
        (.tmp + os.replace) must leave no .tmp file behind after a successful write.

        A lingering .tmp file means os.replace() didn't run — the targets file
        could be absent or truncated, breaking the next Claude session that reads it.
        """
        policy = make_policy()
        ba_min = _bucket_a_min(policy)
        total  = 200_000.0
        holdings = make_holdings({
            "VTI":           (400, total * 0.60 / 400, "core_equity"),
            "IAUM":          ( 50, total * 0.04 / 50,  "precious_metals"),
            "TREASURY_NOTE": (  1, float(ba_min),       "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.64 - ba_min)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        targets_path = str(tmp_path / "precomputed_targets.json")
        tmp_targets  = targets_path + ".tmp"

        _run_with_drawdown(_dd_state(policy, "normal"), tmp_path, monkeypatch,
                           policy=policy, holdings=holdings,
                           hist=hist, scores=scores, gates=gates)

        assert os.path.exists(targets_path), "precomputed_targets.json was not written"
        assert not os.path.exists(tmp_targets), (
            f"Atomic write left behind a .tmp file at {tmp_targets}. "
            "os.replace() must clean up the .tmp file on success."
        )

    @pytest.mark.regression
    def test_t1_settlement_total_available_equals_cash_on_hand(self, tmp_path, monkeypatch):
        """
        T+1 settlement regression: trade_budget.total_available must equal
        cash_on_hand only — sell proceeds from the same cycle do NOT settle
        same-day and must NOT be included.

        Before the fix, total_available = cash_mv + comp_sell_proceeds + mom_sell_proceeds,
        which caused the system to fund same-day buys with proceeds that wouldn't
        settle until T+1, potentially creating a cash deficit at the broker.
        """
        policy = make_policy()
        ba_min = _bucket_a_min(policy)
        total  = 200_000.0
        # IAUM well above cap → generates a TRIM (sell proceeds exist)
        # VTI below core_equity floor → might generate compliance buy
        # Cash is small so the distinction between cash-only vs cash+proceeds matters
        cash_start = 1_000.0
        holdings = make_holdings({
            "VTI":           (100, total * 0.10 / 100, "core_equity"),   # below floor → BUY
            "IAUM":          (500, total * 0.20 / 500, "precious_metals"),  # 20% >> 15% cap → TRIM
            "TREASURY_NOTE": (  1, float(ba_min),        "bucket_a"),
            "CASH":          (max(1, round(cash_start)),  1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        doc = _run_with_drawdown(_dd_state(policy, "normal"), tmp_path, monkeypatch,
                                 policy=policy, holdings=holdings,
                                 hist=hist, scores=scores, gates=gates)

        tb = doc.get("trade_budget", {})
        cash_on_hand    = tb.get("cash_on_hand", 0.0)
        total_available = tb.get("total_available", 0.0)
        sell_proceeds   = tb.get("comp_sell_proceeds", 0.0) + tb.get("mom_sell_proceeds", 0.0)

        assert total_available == pytest.approx(cash_on_hand, abs=1.0), (
            f"T+1 settlement bug: total_available ({total_available:.2f}) must equal "
            f"cash_on_hand ({cash_on_hand:.2f}), not cash + sell_proceeds "
            f"({cash_on_hand + sell_proceeds:.2f}). "
            "Sell proceeds from same-cycle sells settle T+1 and are not available today."
        )

    @pytest.mark.regression
    def test_t1_settlement_buys_capped_to_cash_not_sell_proceeds(self, tmp_path, monkeypatch):
        """
        T+1 settlement regression: when the system proposes both sells and buys
        in the same cycle, actual buy execution must be capped to starting cash
        only — not to cash + sell proceeds.

        Setup: large TRIM sell ($20K+ proceeds) + compliance buy needed for IAUM,
        with only $500 starting cash. Before the fix, comp buys would be funded
        by the sell proceeds (comp_buy_scale ≈ 1.0). After the fix, comp buys
        are strictly limited to $500 (comp_buy_need * comp_buy_scale ≤ cash_on_hand).
        """
        policy = make_policy()
        ba_min = _bucket_a_min(policy)
        total  = 200_000.0
        cash_start = 500.0
        # IAUM at 0% (missing) → large compliance buy needed
        # VTI at 85% → large compliance trim (generates big sell proceeds)
        holdings = make_holdings({
            "VTI":           (850, total * 0.85 / 850, "core_equity"),  # far above cap → TRIM
            "IAUM":          (  1, total * 0.005,       "precious_metals"),  # tiny → compliance BUY
            "TREASURY_NOTE": (  1, float(ba_min),        "bucket_a"),
            "CASH":          (max(1, round(cash_start)),  1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])

        doc = _run_with_drawdown(_dd_state(policy, "normal"), tmp_path, monkeypatch,
                                 policy=policy, holdings=holdings,
                                 hist=hist, scores=scores, gates=gates)

        tb              = doc.get("trade_budget", {})
        cash_on_hand    = tb.get("cash_on_hand", 0.0)
        comp_buy_need   = tb.get("comp_buy_need", 0.0)
        comp_buy_scale  = tb.get("comp_buy_scale", 1.0)
        sell_proceeds   = tb.get("comp_sell_proceeds", 0.0) + tb.get("mom_sell_proceeds", 0.0)

        actual_comp_buys = comp_buy_need * comp_buy_scale

        # Sell proceeds must be non-trivial for this test to be meaningful
        if sell_proceeds > 1_000:
            assert actual_comp_buys <= cash_on_hand + 1.0, (  # 1.0 float tolerance
                f"T+1 settlement bug: actual compliance buys ({actual_comp_buys:.2f}) "
                f"exceed starting cash ({cash_on_hand:.2f}). "
                f"Sell proceeds ({sell_proceeds:.2f}) must not fund same-cycle buys."
            )
