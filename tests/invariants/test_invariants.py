"""
tests/invariants/test_invariants.py

Portfolio-level invariants: assertions that must hold on every run output
regardless of regime or portfolio state.

Gemini Gap 2 additions:
  2a. Cash drag: final cash position ≤ deploy_threshold × TPV (no over-hoarding)
  2b. Universe conformity: all portfolio tickers ⊆ policy universe ∪ {CASH, TREASURY_NOTE}
  2c. Post-trade positions respect min_total and max_total per ticker

Existing invariants (from approved plan):
  - sum of target weights ≈ 1.0 (budget fully allocated)
  - non-negative cash after all trades
  - no L1 / L2 / ticker cap breach in output targets
  - compliance_denom ≤ sizing_denom
  - soft_limit / hard_limit → zero momentum buys
  - DEPLOY only for positive-blend tickers
  - VTI processed last (used as residual absorber)
  - est_usd = 0 for DEFER-BUY rows
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import matplotlib; matplotlib.use("Agg")

from tests.conftest import (
    make_policy,
    make_hist,
    make_holdings,
    make_scores,
    make_gate_rows,
    run_portfolio_tables,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _standard_setup(tmp_path, monkeypatch, drawdown_state="normal",
                    extra_holdings=None, extra_scores=None, extra_gates=None):
    """
    Build a standard ~$100K portfolio and call run_portfolio_tables().

    Returns (doc, tpv) where doc is the parsed JSON and tpv is total portfolio value.
    """
    total = 110_000.0
    holdings = make_holdings({
        "VTI":           (200,  total * 0.50 / 200, "core_equity"),
        "IAUM":          (100,  total * 0.10 / 100, "precious_metals"),
        "TREASURY_NOTE": (  1,  45000.0,             "bucket_a"),
        "CASH":          (500,      1.0,             "cash"),
    })
    if extra_holdings:
        import pandas as pd
        holdings = pd.concat([holdings, extra_holdings], ignore_index=True)

    hist   = make_hist(["VTI", "IAUM"], n_rows=300)
    scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
    if extra_scores:
        import pandas as pd
        scores = pd.concat([scores, extra_scores], ignore_index=True)
    gates  = make_gate_rows(["VTI", "IAUM"])
    if extra_gates:
        import pandas as pd
        gates = pd.concat([gates, extra_gates], ignore_index=True)
    policy = make_policy()

    import mws_runner
    import mws_analytics

    breadth_path  = str(tmp_path / "bs.json")
    tactical_path = str(tmp_path / "tcs.json")
    targets_path  = str(tmp_path / "precomputed_targets.json")
    holdings_csv  = str(tmp_path / "holdings.csv")
    holdings.to_csv(holdings_csv, index=False)

    monkeypatch.setattr(mws_analytics, "BREADTH_STATE_JSON",       breadth_path)
    monkeypatch.setattr(mws_analytics, "TACTICAL_CASH_STATE_JSON",  tactical_path)
    monkeypatch.setattr(mws_analytics, "HOLDINGS_CSV",             holdings_csv)
    monkeypatch.setattr(mws_runner,    "PRECOMPUTED_TARGETS_FILE",  targets_path)

    from tests.conftest import _patch_json_dump
    _patch_json_dump(monkeypatch)

    tpv = float(holdings["MV"].sum())
    analytics = {
        "policy":    policy,
        "holdings":  holdings,
        "hist":      hist,
        "total_val": tpv,
        "val_asof":  str(hist.index.max().date()),
        "drawdown":  {"state": drawdown_state, "drawdown": 0.0 if drawdown_state == "normal" else -0.23,
                      "soft_limit": 0.22, "hard_limit": 0.30},
        "df_scores": scores,
        "df_gates":  gates,
    }
    mws_runner._build_portfolio_tables(analytics)

    with open(targets_path) as f:
        doc = json.load(f)
    return doc, tpv


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPortfolioInvariants:

    # ── Invariant: compliance_denom ≤ sizing_denom ────────────────────────────

    def test_compliance_denom_le_sizing_denom(self, tmp_path, monkeypatch):
        """compliance_denom must always be ≤ sizing_denom."""
        doc, _ = _standard_setup(tmp_path, monkeypatch)
        assert doc["compliance_denom"] <= doc["sizing_denom"] + 1e-6, (
            f"compliance_denom ({doc['compliance_denom']}) > sizing_denom ({doc['sizing_denom']})"
        )

    # ── Invariant: non-negative fields ────────────────────────────────────────

    def test_sizing_denom_non_negative(self, tmp_path, monkeypatch):
        """sizing_denom must be ≥ 0."""
        doc, _ = _standard_setup(tmp_path, monkeypatch)
        assert doc["sizing_denom"] >= 0, f"sizing_denom={doc['sizing_denom']} is negative"

    def test_tpv_positive(self, tmp_path, monkeypatch):
        """TPV must be positive (meaningful portfolio exists)."""
        doc, _ = _standard_setup(tmp_path, monkeypatch)
        assert doc["tpv"] > 0, f"tpv={doc['tpv']} is not positive"

    # ── Invariant 2b: Universe conformity (Gemini Gap 2b) ────────────────────

    def test_portfolio_tickers_in_policy_universe(self, tmp_path, monkeypatch):
        """
        Gap 2b: All tickers in precomputed_targets['portfolio'] must be present in
        policy['ticker_constraints'] or be one of the always-allowed system tickers.

        Ghost tickers (tickers absent from policy but present in output) indicate a
        misconfiguration that could result in unconstrained positions.
        """
        doc, _ = _standard_setup(tmp_path, monkeypatch)
        policy = make_policy()
        allowed = set(policy.get("ticker_constraints", {}).keys()) | {"CASH", "TREASURY_NOTE"}
        portfolio_tickers = set(doc.get("portfolio", {}).keys())
        ghost = portfolio_tickers - allowed
        assert not ghost, (
            f"Gap 2b: ghost tickers in portfolio not in policy universe: {ghost}. "
            "These tickers have no constraints and may violate caps/floors silently."
        )

    # ── Invariant 2c: Post-trade positions respect max_total (Gemini Gap 2c) ─

    def test_post_trade_max_total_not_exceeded(self, tmp_path, monkeypatch):
        """
        Gap 2c: After applying est_buy_usd, estimated final pct must not exceed
        max_total + small tolerance for each ticker.

        This catches bugs where _est_trade() or budget scaling computes a trade
        that would push a ticker above its hard per-ticker cap.
        """
        doc, tpv = _standard_setup(tmp_path, monkeypatch)
        policy = make_policy()
        constraints = policy.get("ticker_constraints", {})
        tol = 0.005  # 0.5pp tolerance for rounding

        violations = []
        for ticker, row in doc.get("portfolio", {}).items():
            max_total = constraints.get(ticker, {}).get("max_total")
            if max_total is None:
                continue
            current_mv = row.get("current_mv", 0) or 0
            est_buy    = row.get("est_buy_usd",  0) or 0
            est_sell   = row.get("est_sell_usd", 0) or 0
            final_mv   = current_mv + est_buy - est_sell
            final_pct  = final_mv / tpv if tpv > 0 else 0
            if final_pct > max_total + tol:
                violations.append(
                    f"{ticker}: final_pct={final_pct:.3f} > max_total={max_total} "
                    f"(buy={est_buy:.0f}, sell={est_sell:.0f}, current={current_mv:.0f})"
                )
        assert not violations, (
            "Gap 2c: post-trade positions exceed max_total:\n" + "\n".join(violations)
        )

    # ── Invariant: est_usd = 0 for DEFER-BUY ─────────────────────────────────

    def test_defer_buy_has_zero_trade_size(self, tmp_path, monkeypatch):
        """DEFER-BUY rows must have est_buy_usd = 0 (trade not executed yet)."""
        # Use a spiked gate to force a DEFER-BUY
        hist   = make_hist(["VTI", "IAUM"], n_rows=300)
        scores = make_scores({"VTI": 0.75, "IAUM": 0.5},
                             tickers_raw={"VTI": 0.05, "IAUM": 0.0})
        # Gate defers VTI buy
        gates  = make_gate_rows(["VTI", "IAUM"],
                                gate_action_buy={"VTI": "defer", "IAUM": "proceed"})
        policy = make_policy()
        # Enable the gate so it actually fires
        policy["execution_gates"]["short_term_confirmation"]["enabled"] = True

        total = 110_000.0
        holdings = make_holdings({
            "VTI":           (100, total * 0.30 / 100, "core_equity"),
            "IAUM":          ( 50, total * 0.09 /  50, "precious_metals"),
            "TREASURY_NOTE": (  1, 45000.0,             "bucket_a"),
            "CASH":          (5000,   1.0,              "cash"),
        })

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)

        for ticker, row in doc.get("portfolio", {}).items():
            if row.get("action") == "DEFER-BUY":
                buy = row.get("est_buy_usd", 0) or 0
                assert buy == 0, (
                    f"DEFER-BUY for {ticker} must have est_buy_usd=0, got {buy}"
                )

    # ── Invariant: soft_limit → no momentum buys ─────────────────────────────

    def test_soft_limit_no_momentum_buys(self, tmp_path, monkeypatch):
        """During soft_limit, no ticker should have action=BUY with basis=momentum_buy."""
        doc, _ = _standard_setup(tmp_path, monkeypatch, drawdown_state="soft_limit")
        for ticker, row in doc.get("portfolio", {}).items():
            if row.get("action") == "BUY":
                basis = row.get("basis", "")
                assert "momentum_buy" not in basis, (
                    f"Invariant: {ticker} has action=BUY basis={basis!r} during soft_limit. "
                    "Momentum buys must be frozen (stress_freeze)."
                )

    # ── Invariant 2a: Cash drag limit (Gemini Gap 2a) ────────────────────────

    def test_cash_not_over_hoarded(self, tmp_path, monkeypatch):
        """
        Gap 2a: Final cash (after all trades) should not exceed a reasonable bound.

        This catches bugs where DEPLOY logic is skipped and cash is left unused
        when there are clearly deployable tickers. Bound: 40% TPV as a generous
        upper limit (tactical_cash_cap_pct = 30%; adding 10pp tolerance).

        Note: we use a generous threshold since the exact deploy bound is policy-
        driven. The key is that cash should not be 100% of TPV after rebalance.
        """
        doc, tpv = _standard_setup(tmp_path, monkeypatch)

        portfolio = doc.get("portfolio", {})
        cash_mv = 0.0
        # Sum current_mv for CASH rows, net of sells (which turn into cash)
        for ticker, row in portfolio.items():
            if ticker == "CASH":
                cash_mv = row.get("current_mv", 0) or 0

        # After trades: add estimated sells, subtract estimated buys across all non-cash tickers
        net_cash_change = 0.0
        for ticker, row in portfolio.items():
            if ticker in ("CASH", "TREASURY_NOTE"):
                continue
            net_cash_change += (row.get("est_sell_usd", 0) or 0)
            net_cash_change -= (row.get("est_buy_usd",  0) or 0)

        final_cash_pct = (cash_mv + net_cash_change) / tpv if tpv > 0 else 0

        # Generous upper bound: 40% (tactical_cash cap is 30%; Bucket A is separate)
        max_cash_pct = 0.40
        assert final_cash_pct <= max_cash_pct + 0.01, (
            f"Gap 2a: estimated final cash {final_cash_pct:.1%} exceeds {max_cash_pct:.0%} TPV. "
            "DEPLOY logic may be suppressed or skipped when deployable cash exists."
        )

    # ── Invariant: hard_limit → no momentum buys ─────────────────────────────

    def test_hard_limit_no_momentum_buys(self, tmp_path, monkeypatch):
        """During hard_limit, no ticker should have action=BUY with basis=momentum_buy."""
        doc, _ = _standard_setup(tmp_path, monkeypatch, drawdown_state="hard_limit")
        for ticker, row in doc.get("portfolio", {}).items():
            if row.get("action") == "BUY":
                basis = row.get("basis", "")
                assert "momentum_buy" not in basis, (
                    f"Invariant: {ticker} has action=BUY basis={basis!r} during hard_limit. "
                    "Momentum buys must be frozen."
                )
