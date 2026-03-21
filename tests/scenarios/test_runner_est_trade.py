"""
tests/scenarios/test_runner_est_trade.py

Tests for _est_trade() trade-size estimation logic inside _build_portfolio_tables().

Regression bugs covered:
  Bug #1: momentum_buy L2 sleeve cap headroom not clamped in _est_trade()
  Bug #2: Multi-ticker sleeve headroom sharing (_sleeve_mom_raw scaling pass)
  Bug #6: Per-ticker max_total not enforced in _est_trade() or DEPLOY loop
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

class TestRunnerEstTrade:

    @pytest.mark.regression
    def test_momentum_buy_capped_by_sleeve_headroom(self, tmp_path, monkeypatch):
        """
        Bug #1 regression: momentum_buy est_usd must not exceed sleeve cap headroom.

        When the strategic_materials sleeve is at 9.5% (cap=10%), the remaining
        headroom is 0.5% × sizing_denom. A momentum buy for URNM must not exceed
        that headroom even if the momentum-proportional target implies a larger trade.
        """
        total = 200_000.0
        # strategic_materials at 9.5% (cap=10%)
        strategic_mv = total * 0.095
        core_mv      = total * 0.55
        holdings = make_holdings({
            "VTI":            (round(core_mv / 230), 230.0,  "core_equity"),
            "URNM":           (round(strategic_mv / 25), 25.0, "strategic_materials"),
            "TREASURY_NOTE":  (1, 45000.0, "bucket_a"),
            "CASH":           (round((total - core_mv - strategic_mv - 45000) / 1.0), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "URNM"], n_rows=300)
        # High momentum for URNM → momentum_buy triggered
        scores = make_scores({"VTI": 0.5, "URNM": 0.90}, tickers_raw={"VTI": 0.05, "URNM": 0.15})
        gates  = make_gate_rows(["VTI", "URNM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        sleeves   = doc.get("sleeves",   {})
        if "URNM" in portfolio and portfolio["URNM"]["action"] == "BUY":
            est_usd = portfolio["URNM"]["est_usd"] or 0.0
            # Compute headroom: (10% cap - 9.5% current) × sizing_denom
            sizing_denom = doc.get("sizing_denom", total)
            strategic_cap = 0.10
            strategic_pct = sleeves.get("strategic_materials", {}).get("current_pct", 9.5) / 100
            headroom = max(0.0, (strategic_cap - strategic_pct) * sizing_denom)
            assert est_usd <= headroom * 1.02, (  # allow 2% float tolerance
                f"Bug #1: momentum_buy est_usd ({est_usd:,.0f}) exceeds sleeve headroom "
                f"({headroom:,.0f}). Trade would push sleeve above cap."
            )

    @pytest.mark.regression
    def test_momentum_buy_zero_when_sleeve_at_exact_cap(self, tmp_path, monkeypatch):
        """
        Bug #1 regression (edge case): when sleeve is exactly at cap, momentum buy
        est_usd must be 0 (or at most the rounding tolerance).
        """
        total = 200_000.0
        cap_pct = 0.10  # strategic_materials cap
        strategic_mv = total * cap_pct  # exactly at cap
        core_mv      = total * 0.55

        holdings = make_holdings({
            "VTI":           (round(core_mv / 230), 230.0,  "core_equity"),
            "URNM":          (round(strategic_mv / 25), 25.0, "strategic_materials"),
            "TREASURY_NOTE": (1, 45000.0, "bucket_a"),
            "CASH":          (max(1, round((total - core_mv - strategic_mv - 45000) / 1.0)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "URNM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "URNM": 0.90}, tickers_raw={"VTI": 0.05, "URNM": 0.15})
        gates  = make_gate_rows(["VTI", "URNM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        if "URNM" in portfolio and portfolio["URNM"]["action"] == "BUY":
            est_usd = portfolio["URNM"]["est_usd"] or 0.0
            sizing_denom = doc.get("sizing_denom", total)
            headroom = max(0.0, cap_pct * sizing_denom - strategic_mv)
            assert est_usd <= headroom + 100, (  # small float tolerance
                f"Bug #1: est_usd ({est_usd:,.0f}) > headroom ({headroom:,.0f}) "
                "when sleeve is at exact cap."
            )

    @pytest.mark.regression
    def test_momentum_buy_capped_by_per_ticker_max_total(self, tmp_path, monkeypatch):
        """
        Bug #6 regression: per-ticker max_total must cap est_usd in _est_trade().

        IAUM has max_total=8% TPV. If IAUM is at 7.5% and a momentum_buy is triggered,
        the buy est_usd should not push IAUM above 8% TPV.
        """
        total = 200_000.0
        # IAUM at 7.5% of TPV (max_total=8%)
        iaum_mv  = total * 0.075
        iaum_px  = 40.0
        core_mv  = total * 0.60

        holdings = make_holdings({
            "VTI":           (round(core_mv / 230), 230.0, "core_equity"),
            "IAUM":          (round(iaum_mv / iaum_px), iaum_px, "precious_metals"),
            "TREASURY_NOTE": (1, 45000.0, "bucket_a"),
            "CASH":          (max(1, round((total - core_mv - iaum_mv - 45000) / 1.0)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.85}, tickers_raw={"VTI": 0.05, "IAUM": 0.12})
        gates  = make_gate_rows(["VTI", "IAUM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        tpv       = doc.get("tpv", total)
        if "IAUM" in portfolio and portfolio["IAUM"]["action"] == "BUY":
            est_usd  = portfolio["IAUM"]["est_usd"] or 0.0
            max_room = max(0.0, 0.08 * tpv - iaum_mv)   # 8% TPV - current MV
            assert est_usd <= max_room * 1.02, (  # 2% float tolerance
                f"Bug #6: IAUM est_usd ({est_usd:,.0f}) exceeds per-ticker max_total "
                f"headroom ({max_room:,.0f}). Would push ticker above 8% TPV limit."
            )

    @pytest.mark.regression
    def test_multi_ticker_sleeve_buys_do_not_exceed_headroom(self, tmp_path, monkeypatch):
        """
        Bug #2 regression: when multiple tickers in the same sleeve both get
        momentum_buy signals, their combined est_usd must not exceed sleeve headroom.

        Without the _sleeve_mom_raw scaling pass, each ticker independently sees
        the full sleeve headroom and their combined buys exceed the sleeve cap.
        """
        total = 200_000.0
        # precious_metals at 12% (cap=15%) — headroom = 3%
        pm_mv      = total * 0.12
        core_mv    = total * 0.55
        iaum_px    = 40.0
        sivr_px    = 20.0
        # Split evenly between IAUM and SIVR
        iaum_mv    = pm_mv * 0.5
        sivr_mv    = pm_mv * 0.5

        holdings = make_holdings({
            "VTI":           (round(core_mv / 230), 230.0, "core_equity"),
            "IAUM":          (round(iaum_mv / iaum_px), iaum_px, "precious_metals"),
            "SIVR":          (round(sivr_mv / sivr_px), sivr_px, "precious_metals"),
            "TREASURY_NOTE": (1, 45000.0, "bucket_a"),
            "CASH":          (max(1, round((total - core_mv - pm_mv - 45000) / 1.0)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM", "SIVR"], n_rows=300)
        # Both IAUM and SIVR have high momentum → both get momentum_buy
        scores = make_scores(
            {"VTI": 0.5, "IAUM": 0.80, "SIVR": 0.75},
            tickers_raw={"VTI": 0.05, "IAUM": 0.08, "SIVR": 0.06},
        )
        gates  = make_gate_rows(["VTI", "IAUM", "SIVR"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio    = doc.get("portfolio", {})
        sleeves      = doc.get("sleeves",   {})
        sizing_denom = doc.get("sizing_denom", total)

        # Sum est_usd for all momentum buys in precious_metals
        pm_cap     = 0.15
        pm_current = sleeves.get("precious_metals", {}).get("current_pct", 12.0) / 100
        headroom   = max(0.0, (pm_cap - pm_current) * sizing_denom)

        total_buy = 0.0
        for t in ["IAUM", "SIVR"]:
            if t in portfolio:
                entry = portfolio[t]
                if entry["action"] in ("BUY",) and "momentum_buy" in entry.get("basis", ""):
                    total_buy += entry.get("est_usd") or 0.0

        if total_buy > 0:
            assert total_buy <= headroom * 1.02, (
                f"Bug #2: combined momentum buys ({total_buy:,.0f}) for IAUM + SIVR "
                f"exceed sleeve headroom ({headroom:,.0f}). "
                "_sleeve_mom_raw scaling pass should prevent this."
            )
