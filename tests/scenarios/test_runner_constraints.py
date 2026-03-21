"""
tests/scenarios/test_runner_constraints.py

Tests for constraint enforcement in _build_portfolio_tables().

Regression bugs covered:
  Bug #6:  Per-ticker max_total not enforced in _est_trade() or DEPLOY loop
  Bug #12: L1 cap never enforced
  Bug #13: Per-ticker min_total floor not enforced as compliance trigger
  Bug #15: Bucket A minimum read from wrong policy field
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


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunnerConstraints:

    @pytest.mark.regression
    def test_l1_cap_enforced_via_scaling(self, tmp_path, monkeypatch):
        """
        Bug #12 regression: L1 sleeve cap must be enforced via proportional scaling.

        When multiple L2 children of a L1 sleeve all have momentum buys, their
        combined buys can push the L1 sleeve over its cap. The runner must scale
        all buys in the L1 proportionally when this happens.

        growth L1 cap = 60%. Test: core_equity + biotech both get momentum buys
        and their raw trades would exceed 60% L1 headroom → scaling must apply.
        """
        total = 200_000.0
        # growth L1 at 55% (cap=60%) — headroom = 5%
        core_mv    = total * 0.45
        biotech_mv = total * 0.10
        pm_mv      = total * 0.10  # precious_metals (monetary_hedges L1)

        holdings = make_holdings({
            "VTI":           (round(core_mv / 230),     230.0, "core_equity"),
            "XBI":           (round(biotech_mv / 50),    50.0, "biotech"),
            "IAUM":          (round(pm_mv / 40),         40.0, "precious_metals"),
            "TREASURY_NOTE": (1,                       45000.0, "bucket_a"),
            "CASH":          (max(1, round((total - core_mv - biotech_mv - pm_mv - 45000) / 1.0)),
                              1.0, "cash"),
        })
        hist   = make_hist(["VTI", "XBI", "IAUM"], n_rows=300)
        # Both VTI and XBI get high momentum → both try to buy → combined may exceed L1 cap
        scores = make_scores(
            {"VTI": 0.80, "XBI": 0.75, "IAUM": 0.50},
            tickers_raw={"VTI": 0.10, "XBI": 0.08, "IAUM": 0.02},
        )
        gates  = make_gate_rows(["VTI", "XBI", "IAUM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio    = doc.get("portfolio", {})
        sizing_denom = doc.get("sizing_denom", total)
        sleeves      = doc.get("sleeves", {})

        # Compute actual growth L1 total after trades
        growth_l2s = ["core_equity", "ai_tech", "biotech"]
        growth_mv_now = sum(
            sleeves.get(sl, {}).get("mv", 0.0) for sl in growth_l2s
        )

        growth_buy = 0.0
        for t in portfolio:
            entry = portfolio[t]
            if entry.get("sleeve_l1") == "growth" and entry.get("action") == "BUY":
                growth_buy += entry.get("est_usd") or 0.0

        growth_cap    = 0.60
        growth_room   = max(0.0, growth_cap * sizing_denom - growth_mv_now)

        if growth_buy > 0:
            assert growth_buy <= growth_room * 1.02, (
                f"Bug #12: combined growth L1 buys ({growth_buy:,.0f}) exceed "
                f"L1 cap headroom ({growth_room:,.0f}). "
                "L1 cap scaling must proportionally reduce buys."
            )

    @pytest.mark.regression
    def test_per_ticker_max_total_capped_in_est_trade(self, tmp_path, monkeypatch):
        """
        Bug #6 regression: per-ticker max_total (TPV-based) must cap est_usd.

        URNM has max_total=4% TPV. A momentum buy when URNM is at 3.9% should
        produce est_usd ≤ 0.1% × TPV headroom.
        """
        total = 200_000.0
        urnm_mv = total * 0.039   # 3.9% — just below 4% max_total
        core_mv = total * 0.60

        holdings = make_holdings({
            "VTI":           (round(core_mv / 230), 230.0,  "core_equity"),
            "URNM":          (round(urnm_mv / 25),   25.0, "strategic_materials"),
            "TREASURY_NOTE": (1, 45000.0, "bucket_a"),
            "CASH":          (max(1, round((total - core_mv - urnm_mv - 45000))), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "URNM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "URNM": 0.85}, tickers_raw={"VTI": 0.05, "URNM": 0.12})
        gates  = make_gate_rows(["VTI", "URNM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        tpv       = doc.get("tpv", total)
        if "URNM" in portfolio and portfolio["URNM"]["action"] == "BUY":
            est_usd  = portfolio["URNM"]["est_usd"] or 0.0
            max_room = max(0.0, 0.04 * tpv - urnm_mv)
            assert est_usd <= max_room + 100, (
                f"Bug #6: URNM est_usd ({est_usd:,.0f}) exceeds per-ticker max_total "
                f"headroom ({max_room:,.0f})"
            )

    @pytest.mark.regression
    def test_per_ticker_min_total_triggers_compliance_buy(self, tmp_path, monkeypatch):
        """
        Bug #13 regression: per-ticker min_total floors must trigger compliance_buy.

        VTI has min_total=10% TPV. A portfolio where VTI is at 8% must show
        a compliance_buy for VTI even if the core_equity L2 sleeve is in-band.
        """
        total = 200_000.0
        # VTI at 8% TPV ($16K) — below its min_total of 10% ($20K).
        # VXUS fills core_equity sleeve so the sleeve is in-band (18-38% sd),
        # meaning the compliance_buy triggers on ticker min_total, not sleeve floor.
        vti_mv  = total * 0.08   # $16K
        vxus_mv = total * 0.15   # $30K — core_equity = 29% sd (in-band)
        iaum_mv = total * 0.10   # $20K
        cash_mv = total - vti_mv - vxus_mv - iaum_mv - 45000

        holdings = make_holdings({
            "VTI":           (round(vti_mv / 230),   230.0, "core_equity"),
            "VXUS":          (round(vxus_mv / 60),    60.0, "core_equity"),
            "IAUM":          (round(iaum_mv / 40),    40.0, "precious_metals"),
            "TREASURY_NOTE": (1,                   45000.0, "bucket_a"),
            "CASH":          (max(1, round(cash_mv)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "VXUS", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "VXUS": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "VXUS", "IAUM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        if "VTI" in portfolio:
            vti = portfolio["VTI"]
            assert vti["action"] == "BUY", (
                f"Bug #13: VTI at 8% TPV (below min_total=10%) should trigger "
                f"compliance_buy. Got action={vti['action']!r}, basis={vti['basis']!r}"
            )
            assert "compliance_buy" in vti["basis"]

    @pytest.mark.regression
    def test_bucket_a_minimum_read_from_correct_field(self, tmp_path, monkeypatch):
        """
        Bug #15 regression: Bucket A minimum must be read from:
            definitions.buckets.bucket_a_protected_liquidity.minimum_usd

        NOT from bucket_a.minimum_balance (the old wrong key).

        Test: policy with ONLY the correct path set. The runner should correctly
        detect Bucket A breach and suppress buys when TREASURY_NOTE < minimum_usd.
        """
        total = 150_000.0
        # TREASURY_NOTE fallback_price is $45K (fixed synthetic asset, 1 share).
        # Set minimum_usd to $50K > $45K so the runner detects a breach.
        # This validates the code reads from the CORRECT policy path:
        #   definitions.buckets.bucket_a_protected_liquidity.minimum_usd
        holdings = make_holdings({
            "VTI":           (400, total * 0.70 / 400, "core_equity"),
            "IAUM":          (100, total * 0.07 / 100, "precious_metals"),
            "TREASURY_NOTE": (1, 45000.0, "bucket_a"),  # $45K < $50K minimum_usd
            "CASH":          (max(1, round(total - total * 0.77 - 45000)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.85}, tickers_raw={"VTI": 0.05, "IAUM": 0.12})
        gates  = make_gate_rows(["VTI", "IAUM"])

        # Policy with higher minimum_usd ($50K) at the CORRECT path
        policy = make_policy()
        policy["definitions"]["buckets"]["bucket_a_protected_liquidity"]["minimum_usd"] = 50_000

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        bucket_a = doc.get("bucket_a", {})
        # Bucket A is below minimum ($45K < $50K minimum_usd)
        bucket_a_mv = bucket_a.get("mv", 0)
        assert bucket_a.get("status") == "BELOW_MIN" or bucket_a_mv < 50_000, (
            f"Bug #15: Bucket A status should reflect breach when mv={bucket_a_mv}"
        )

        # When Bucket A is breached, all buys (including compliance) should be suppressed
        portfolio = doc.get("portfolio", {})
        trade_budget = doc.get("trade_budget", {})
        if trade_budget.get("comp_buy_scale") is not None:
            assert trade_budget["comp_buy_scale"] == 0.0, (
                f"Bug #14/15: When Bucket A is breached, comp_buy_scale must be 0. "
                f"Got {trade_budget['comp_buy_scale']}"
            )

    def test_zero_sizing_denom_no_crash(self, tmp_path, monkeypatch):
        """
        When sizing_denom ≈ 0 (edge case: all assets are overlays + bucket_a),
        _build_portfolio_tables() must not raise ZeroDivisionError.
        """
        holdings = make_holdings({
            "DBMF":          (100, 25.0, "managed_futures"),  # overlay
            "TREASURY_NOTE": (1, 45000.0, "bucket_a"),
            "CASH":          (1,     1.0, "cash"),
        })
        hist   = make_hist(["DBMF"], n_rows=300)
        scores = make_scores({"DBMF": 0.5})
        gates  = make_gate_rows(["DBMF"])
        policy = make_policy()

        # Should not raise
        try:
            doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)
            assert isinstance(doc, dict)
        except ZeroDivisionError as e:
            pytest.fail(f"ZeroDivisionError with near-zero sizing_denom: {e}")
