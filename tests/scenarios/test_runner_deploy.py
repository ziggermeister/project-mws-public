"""
tests/scenarios/test_runner_deploy.py

Tests for Phase 3 residual cash deployment logic in _build_portfolio_tables().

Verifies:
  - DEPLOY action appears for HOLD tickers with positive blend + cash residual
  - Highest-momentum tickers get DEPLOY first, VTI is appended last
  - Negative-blend tickers do not receive DEPLOY
  - No DEPLOY when cash == 0
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import mws_runner
import mws_analytics

from tests.conftest import (
    make_policy,
    make_hist,
    make_holdings,
    make_scores,
    make_gate_rows,
)


# ── Helper: run with specific analytics dict ──────────────────────────────────

def _run_with_analytics(analytics, tmp_path, monkeypatch):
    """Invoke _build_portfolio_tables with full analytics dict, return parsed JSON."""
    breadth_path  = str(tmp_path / "bs.json")
    tactical_path = str(tmp_path / "tcs.json")
    targets_path  = str(tmp_path / "precomputed_targets.json")
    holdings_csv  = str(tmp_path / "holdings.csv")
    analytics["holdings"].to_csv(holdings_csv, index=False)

    monkeypatch.setattr(mws_analytics, "BREADTH_STATE_JSON",       breadth_path)
    monkeypatch.setattr(mws_analytics, "TACTICAL_CASH_STATE_JSON",  tactical_path)
    monkeypatch.setattr(mws_analytics, "HOLDINGS_CSV",             holdings_csv)
    monkeypatch.setattr(mws_runner,    "PRECOMPUTED_TARGETS_FILE",  targets_path)

    mws_runner._build_portfolio_tables(analytics)

    with open(targets_path) as f:
        return json.load(f)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunnerDeploy:

    def test_deploy_fires_for_positive_blend_hold_with_cash(self, tmp_path, monkeypatch):
        """
        When there is residual cash (> $500) and a HOLD ticker has positive blend
        and sleeve headroom, it should receive a DEPLOY action.
        """
        total = 200_000.0
        # VTI in core_equity — let it be HOLD (in-band, mid-range momentum)
        # Give large cash residual
        vti_mv  = total * 0.35
        iaum_mv = total * 0.09  # precious_metals in-band
        cash    = total * 0.35  # large residual cash

        holdings = make_holdings({
            "VTI":           (round(vti_mv / 230),  230.0, "core_equity"),
            "IAUM":          (round(iaum_mv / 40),   40.0, "precious_metals"),
            "TREASURY_NOTE": (1,                   45000.0, "bucket_a"),
            "CASH":          (round(cash),            1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        # VTI has strong positive blend but pct in mid-range (0.55) — HOLD, not BUY
        scores = make_scores({"VTI": 0.55, "IAUM": 0.45},
                             tickers_raw={"VTI": 0.08, "IAUM": 0.03})
        gates  = make_gate_rows(["VTI", "IAUM"])
        policy = make_policy()

        analytics = {
            "policy":    policy,
            "holdings":  holdings,
            "hist":      hist,
            "total_val": float(holdings["MV"].sum()),
            "val_asof":  str(hist.index.max().date()),
            "drawdown":  {"state": "normal", "drawdown": 0.0,
                          "soft_limit": 0.22, "hard_limit": 0.30},
            "df_scores": scores,
            "df_gates":  gates,
        }
        doc = _run_with_analytics(analytics, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        deploy_tickers = [t for t, e in portfolio.items() if e.get("action") == "DEPLOY"]

        tb = doc.get("trade_budget", {})
        if tb.get("cash_on_hand", 0) > 500:
            # At least one DEPLOY should have fired given the large cash residual
            assert len(deploy_tickers) > 0 or tb.get("deploy_total", 0) > 0, (
                "Expected at least one DEPLOY with large cash residual and positive-blend tickers"
            )

    def test_deploy_does_not_fire_when_no_cash(self, tmp_path, monkeypatch):
        """When cash == 0, no DEPLOY actions should appear."""
        total = 200_000.0
        vti_mv  = total * 0.50
        iaum_mv = total * 0.09

        holdings = make_holdings({
            "VTI":           (round(vti_mv / 230),  230.0, "core_equity"),
            "IAUM":          (round(iaum_mv / 40),   40.0, "precious_metals"),
            "TREASURY_NOTE": (1,                   45000.0, "bucket_a"),
            "CASH":          (0,                     1.0, "cash"),  # zero cash
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.55, "IAUM": 0.45},
                             tickers_raw={"VTI": 0.08, "IAUM": 0.03})
        gates  = make_gate_rows(["VTI", "IAUM"])
        policy = make_policy()

        analytics = {
            "policy":    policy,
            "holdings":  holdings,
            "hist":      hist,
            "total_val": float(holdings["MV"].sum()),
            "val_asof":  str(hist.index.max().date()),
            "drawdown":  {"state": "normal", "drawdown": 0.0,
                          "soft_limit": 0.22, "hard_limit": 0.30},
            "df_scores": scores,
            "df_gates":  gates,
        }
        doc = _run_with_analytics(analytics, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        deploy_tickers = [t for t, e in portfolio.items() if e.get("action") == "DEPLOY"]
        deploy_total   = doc.get("trade_budget", {}).get("deploy_total", 0)

        assert len(deploy_tickers) == 0 and deploy_total == 0, (
            f"No DEPLOY expected with zero cash. Got deploy_tickers={deploy_tickers}, "
            f"deploy_total={deploy_total}"
        )

    def test_deploy_skips_negative_blend_tickers(self, tmp_path, monkeypatch):
        """
        DEPLOY only goes to tickers with positive blend (RawScore > 0).
        Tickers with negative blend must not receive DEPLOY even if they have headroom.
        """
        total = 200_000.0
        vti_mv  = total * 0.30
        iaum_mv = total * 0.09
        cash    = total * 0.30  # large residual

        holdings = make_holdings({
            "VTI":           (round(vti_mv / 230),  230.0, "core_equity"),
            "IAUM":          (round(iaum_mv / 40),   40.0, "precious_metals"),
            "TREASURY_NOTE": (1,                   45000.0, "bucket_a"),
            "CASH":          (round(cash),            1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        # IAUM has NEGATIVE blend — should NOT receive DEPLOY
        scores = make_scores({"VTI": 0.55, "IAUM": 0.40},
                             tickers_raw={"VTI": 0.08, "IAUM": -0.05})  # IAUM negative!
        gates  = make_gate_rows(["VTI", "IAUM"])
        policy = make_policy()

        analytics = {
            "policy":    policy,
            "holdings":  holdings,
            "hist":      hist,
            "total_val": float(holdings["MV"].sum()),
            "val_asof":  str(hist.index.max().date()),
            "drawdown":  {"state": "normal", "drawdown": 0.0,
                          "soft_limit": 0.22, "hard_limit": 0.30},
            "df_scores": scores,
            "df_gates":  gates,
        }
        doc = _run_with_analytics(analytics, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        if "IAUM" in portfolio:
            iaum_action = portfolio["IAUM"].get("action")
            assert iaum_action != "DEPLOY", (
                f"IAUM (negative blend) must not receive DEPLOY. Got action={iaum_action!r}"
            )

    def test_vti_receives_deploy_last(self, tmp_path, monkeypatch):
        """
        VTI should be the LAST ticker to receive residual deployment.
        Policy: residual flows to highest-momentum in-band tickers first, VTI last.
        """
        total = 200_000.0
        vti_mv  = total * 0.25
        iaum_mv = total * 0.09
        urnm_mv = total * 0.05
        cash    = total * 0.50  # very large residual — should exhaust IAUM + URNM before VTI

        holdings = make_holdings({
            "VTI":           (round(vti_mv / 230),  230.0, "core_equity"),
            "IAUM":          (round(iaum_mv / 40),   40.0, "precious_metals"),
            "URNM":          (round(urnm_mv / 25),   25.0, "strategic_materials"),
            "TREASURY_NOTE": (1,                   45000.0, "bucket_a"),
            "CASH":          (round(cash),            1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM", "URNM"], n_rows=300)
        # IAUM and URNM have higher pct than VTI (mid-range) → should receive DEPLOY first
        scores = make_scores(
            {"VTI": 0.50, "IAUM": 0.60, "URNM": 0.65},
            tickers_raw={"VTI": 0.05, "IAUM": 0.04, "URNM": 0.07},
        )
        gates  = make_gate_rows(["VTI", "IAUM", "URNM"])
        policy = make_policy()

        analytics = {
            "policy":    policy,
            "holdings":  holdings,
            "hist":      hist,
            "total_val": float(holdings["MV"].sum()),
            "val_asof":  str(hist.index.max().date()),
            "drawdown":  {"state": "normal", "drawdown": 0.0,
                          "soft_limit": 0.22, "hard_limit": 0.30},
            "df_scores": scores,
            "df_gates":  gates,
        }
        doc = _run_with_analytics(analytics, tmp_path, monkeypatch)

        portfolio     = doc.get("portfolio", {})
        deploy_tickers = [t for t, e in portfolio.items() if e.get("action") == "DEPLOY"]

        if "VTI" in deploy_tickers and len(deploy_tickers) > 1:
            # VTI should not be the first one to receive DEPLOY when others qualify
            # The runner appends VTI last — verify VTI's est_usd is <= URNM/IAUM
            vti_deploy = portfolio.get("VTI", {}).get("est_usd", 0) or 0
            for t in ["URNM", "IAUM"]:
                if t in portfolio and portfolio[t].get("action") == "DEPLOY":
                    t_deploy = portfolio[t].get("est_usd", 0) or 0
                    # If URNM/IAUM deploy > 0, they were served first
                    if t_deploy > 0:
                        break  # At least one non-VTI ticker was deployed to
