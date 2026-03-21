"""
tests/scenarios/test_runner_action.py

Tests for _action() logic inside _build_portfolio_tables().

Tested indirectly by calling _build_portfolio_tables() (via run_portfolio_tables()
conftest helper) and asserting on the mws_precomputed_targets.json output.

Regression bugs covered:
  Bug #8:  soft_limit buy freeze not enforced in _action()
  Bug #11: Compliance buys incorrectly deferred by execution gate
  Bug #13: Per-ticker min_total floor not enforced as compliance trigger
"""
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _base_holdings():
    """Holdings where sleeves are roughly in-band (VTI dominant, small IAUM and URNM)."""
    return make_holdings({
        "VTI":            (100,  230.0, "core_equity"),
        "IAUM":           ( 50,   40.0, "precious_metals"),
        "URNM":           ( 30,   25.0, "strategic_materials"),
        "DBMF":           ( 50,   25.0, "managed_futures"),
        "TREASURY_NOTE":  (  1, 45000.0, "bucket_a"),
        "CASH":           (1000,   1.0, "cash"),
    })


def _all_tickers():
    return ["VTI", "IAUM", "URNM", "DBMF"]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunnerAction:

    def test_sleeve_below_floor_triggers_compliance_buy(self, tmp_path, monkeypatch):
        """
        When sleeve current_pct < floor − 0.1pp, the ticker should have
        action='BUY' and basis containing 'compliance_buy'.
        """
        # Precious metals floor = 8%, give IAUM only ~1% of portfolio
        total = 100_000.0
        holdings = make_holdings({
            "VTI":            (400, total * 0.88 / 400, "core_equity"),
            "IAUM":           (  1,       total * 0.01, "precious_metals"),  # only 1%
            "TREASURY_NOTE":  (  1,              45000, "bucket_a"),
            "CASH":           (1000,                1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        # Codex P1: assert precondition first — no optional guard
        portfolio = doc.get("portfolio", {})
        assert "IAUM" in portfolio, "IAUM must appear in portfolio output (regression: ticker disappeared)"
        iaum = portfolio["IAUM"]
        assert iaum["action"] == "BUY", (
            f"IAUM at {iaum['current_pct']:.1f}% should be a compliance BUY "
            f"(precious_metals floor={iaum['floor_pct']:.0f}%)"
        )
        assert "compliance_buy" in iaum["basis"], (
            f"Basis should be 'compliance_buy', got {iaum['basis']!r}"
        )

    def test_sleeve_above_cap_triggers_compliance_trim(self, tmp_path, monkeypatch):
        """
        When sleeve current_pct > cap + 0.1pp, the ticker should have
        action='TRIM' and basis containing 'compliance_trim'.
        """
        total = 100_000.0
        # Put IAUM way above precious_metals cap (15%)
        holdings = make_holdings({
            "VTI":            (200, total * 0.60 / 200, "core_equity"),
            "IAUM":           (500,          total * 0.20 / 500, "precious_metals"),  # 20% > 15% cap
            "TREASURY_NOTE":  (  1,              45000,  "bucket_a"),
            "CASH":           (  0,                 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "IAUM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        assert "IAUM" in portfolio, "IAUM must appear in portfolio output"
        iaum = portfolio["IAUM"]
        assert iaum["action"] == "TRIM", (
            f"IAUM at {iaum['current_pct']:.1f}% above cap {iaum['cap_pct']:.0f}% "
            "should be a compliance TRIM"
        )
        assert "compliance_trim" in iaum["basis"]

    @pytest.mark.regression
    def test_per_ticker_min_total_triggers_compliance_buy(self, tmp_path, monkeypatch):
        """
        Bug #13 regression: per-ticker min_total floor must trigger compliance_buy
        even when the L2 sleeve is in-band.

        VTI has min_total=10% TPV. If VTI is at 8% of TPV, a compliance_buy
        should fire for VTI even if the core_equity sleeve is within its floor/cap.
        """
        total = 200_000.0
        # VTI at 8% TPV ($16K) — below its min_total of 10% ($20K).
        # VXUS added so core_equity sleeve is in-band (18–38% of sizing_denom),
        # ensuring the compliance_buy fires for ticker min_total, not sleeve floor.
        vti_mv  = total * 0.08   # $16K → 8% TPV < 10% min_total
        vxus_mv = total * 0.15   # $30K → core_equity total = 23% sizing_denom (in-band)
        iaum_mv = total * 0.10   # $20K → in precious_metals band
        cash_mv = total - vti_mv - vxus_mv - iaum_mv - 45000
        holdings = make_holdings({
            "VTI":           (round(vti_mv / 230),   230.0, "core_equity"),
            "VXUS":          (round(vxus_mv / 60),    60.0, "core_equity"),
            "IAUM":          (round(iaum_mv / 40),    40.0, "precious_metals"),
            "TREASURY_NOTE": (  1,                 45000.0, "bucket_a"),
            "CASH":          (max(1, round(cash_mv)), 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "VXUS", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "VXUS": 0.5, "IAUM": 0.5})
        gates  = make_gate_rows(["VTI", "VXUS", "IAUM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        assert "VTI" in portfolio, "VTI must appear in portfolio output"
        vti = portfolio["VTI"]
        assert vti["action"] == "BUY", (
            f"VTI at {vti['current_pct']:.1f}% should trigger compliance_buy "
            f"(min_total=10% TPV). Got action={vti['action']!r}, basis={vti['basis']!r}"
        )
        assert "compliance_buy" in vti["basis"], (
            f"Bug #13: per-ticker min_total should trigger compliance_buy. "
            f"Got basis={vti['basis']!r}"
        )

    @pytest.mark.regression
    def test_compliance_buy_not_deferred_by_gate(self, tmp_path, monkeypatch):
        """
        Bug #11 regression: compliance buys must NOT be deferred by execution gate.

        Policy says compliance buys (floor enforcement) are exempt from the gate
        (execution_gates._meta.does_not_apply_to: cap_floor_compliance).
        A z-score spike should defer momentum_buy but never defer compliance_buy.
        """
        total = 100_000.0
        # IAUM at 1% — deep below precious_metals floor of 8% → compliance_buy
        holdings = make_holdings({
            "VTI":           (400, total * 0.89 / 400, "core_equity"),
            "IAUM":          (  1,           total * 0.01, "precious_metals"),
            "TREASURY_NOTE": (  1,           45000, "bucket_a"),
            "CASH":          (1000,            1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        # Set gate_action_buy to "defer" for IAUM — should NOT affect compliance buys
        gates  = make_gate_rows(["VTI", "IAUM"],
                                gate_action_buy={"VTI": "proceed", "IAUM": "defer"})
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        assert "IAUM" in portfolio, "IAUM must appear in portfolio output"
        iaum = portfolio["IAUM"]
        assert iaum["action"] != "DEFER-BUY", (
            f"Bug #11: compliance buy must not be deferred by gate. "
            f"IAUM action={iaum['action']!r}, basis={iaum['basis']!r}. "
            "Execution gate exempts cap_floor_compliance."
        )
        assert iaum["action"] == "BUY", (
            f"IAUM should be BUY (compliance), got {iaum['action']!r}"
        )

    @pytest.mark.regression
    def test_momentum_buy_blocked_during_soft_limit(self, tmp_path, monkeypatch):
        """
        Bug #8 regression: momentum buys must be blocked (HOLD) during soft_limit.

        Policy: soft_limit freezes new (momentum) buys; only compliance buys remain.
        A ticker with high momentum pct (>= 0.65) should return HOLD (stress_freeze)
        when drawdown state is soft_limit.
        """
        total = 100_000.0
        holdings = make_holdings({
            "VTI":           (300, total * 0.60 / 300, "core_equity"),
            "IAUM":          (200, total * 0.12 / 200, "precious_metals"),
            "TREASURY_NOTE": (  1,              45000, "bucket_a"),
            "CASH":          (1000,              1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        # IAUM has high momentum pct (0.80) and positive RawScore → normally momentum_buy
        scores = make_scores({"VTI": 0.5, "IAUM": 0.80}, tickers_raw={"VTI": 0.05, "IAUM": 0.10})
        gates  = make_gate_rows(["VTI", "IAUM"])
        policy = make_policy()

        import mws_runner
        breadth_path  = str(tmp_path / "bs.json")
        tactical_path = str(tmp_path / "tcs.json")
        targets_path  = str(tmp_path / "precomputed_targets.json")
        holdings_csv  = str(tmp_path / "holdings.csv")
        holdings.to_csv(holdings_csv, index=False)

        import mws_analytics
        monkeypatch.setattr(mws_analytics, "BREADTH_STATE_JSON",       breadth_path)
        monkeypatch.setattr(mws_analytics, "TACTICAL_CASH_STATE_JSON",  tactical_path)
        monkeypatch.setattr(mws_analytics, "HOLDINGS_CSV",             holdings_csv)
        monkeypatch.setattr(mws_runner,    "PRECOMPUTED_TARGETS_FILE",  targets_path)

        # Inject soft_limit drawdown state
        analytics = {
            "policy":    policy,
            "holdings":  holdings,
            "hist":      hist,
            "total_val": float(holdings["MV"].sum()),
            "val_asof":  str(hist.index.max().date()),
            "drawdown":  {"state": "soft_limit", "drawdown": -0.23,
                          "soft_limit": 0.22, "hard_limit": 0.30},
            "df_scores": scores,
            "df_gates":  gates,
        }
        mws_runner._build_portfolio_tables(analytics)

        import json
        with open(targets_path) as f:
            doc = json.load(f)

        portfolio = doc.get("portfolio", {})
        # IAUM is in precious_metals — currently at 12% which is within the 8%-15% band.
        # With high momentum (pct=0.80) but soft_limit active → should be HOLD not BUY
        # Codex P1: assert precondition first, then assert the intended invariant
        assert "IAUM" in portfolio, "IAUM must appear in portfolio output"
        iaum = portfolio["IAUM"]
        # Regardless of basis, momentum buys must never fire during soft_limit
        assert iaum["action"] != "BUY" or "momentum" not in iaum.get("basis", ""), (
            f"Bug #8: momentum buy should be blocked during soft_limit. "
            f"IAUM action={iaum['action']!r}, basis={iaum['basis']!r}"
        )

    def test_conflicting_signal_floor_wins_over_low_pct(self, tmp_path, monkeypatch):
        """
        Conflicting signals: sleeve below floor (normally BUY) + ticker has low pct
        (which would normally trigger a trim). The compliance_buy should win.

        Floor enforcement (Priority 3) overrides momentum trim (Priority 5).
        """
        total = 100_000.0
        holdings = make_holdings({
            "VTI":           (400, total * 0.89 / 400, "core_equity"),
            "IAUM":          (  1,  total * 0.01, "precious_metals"),  # below floor
            "TREASURY_NOTE": (  1,  45000, "bucket_a"),
            "CASH":          (1000,   1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        # IAUM has LOW pct (0.20) — would be momentum_trim — but sleeve is below floor
        scores = make_scores({"VTI": 0.5, "IAUM": 0.20})
        gates  = make_gate_rows(["VTI", "IAUM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)
        portfolio = doc.get("portfolio", {})
        assert "IAUM" in portfolio, "IAUM must appear in portfolio output"
        iaum = portfolio["IAUM"]
        # Floor breach → compliance_buy wins over momentum_trim
        assert iaum["action"] == "BUY", (
            f"Compliance BUY (floor enforcement) must override momentum_trim signal. "
            f"IAUM action={iaum['action']!r}, basis={iaum['basis']!r}"
        )
        assert "compliance_buy" in iaum["basis"]

    def test_conflicting_signal_cap_wins_over_high_pct(self, tmp_path, monkeypatch):
        """
        Conflicting signals: sleeve above cap (normally TRIM) + ticker has high pct
        (which would normally trigger a buy). The compliance_trim should win.
        """
        total = 100_000.0
        # IAUM at 20% — above precious_metals cap of 15%
        holdings = make_holdings({
            "VTI":           (200, total * 0.60 / 200, "core_equity"),
            "IAUM":          (200,  total * 0.20 / 200, "precious_metals"),
            "TREASURY_NOTE": (  1,               45000, "bucket_a"),
            "CASH":          (  0,                 1.0, "cash"),
        })
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        # IAUM has HIGH pct (0.80) — would be momentum_buy — but sleeve is above cap
        scores = make_scores({"VTI": 0.5, "IAUM": 0.80}, tickers_raw={"VTI": 0.05, "IAUM": 0.10})
        gates  = make_gate_rows(["VTI", "IAUM"])
        policy = make_policy()

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)
        portfolio = doc.get("portfolio", {})
        assert "IAUM" in portfolio, "IAUM must appear in portfolio output"
        iaum = portfolio["IAUM"]
        # Cap breach → compliance_trim wins over momentum_buy
        assert iaum["action"] == "TRIM", (
            f"Compliance TRIM (cap enforcement) must override momentum_buy signal. "
            f"IAUM action={iaum['action']!r}, basis={iaum['basis']!r}"
        )
        assert "compliance_trim" in iaum["basis"]
