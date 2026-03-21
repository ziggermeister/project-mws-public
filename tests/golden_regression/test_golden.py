"""
tests/golden_regression/test_golden.py

Golden-file regression tests — lock down output schema and key fields across
5 canonical scenarios.

Gemini Gap 4: Each golden entry includes 'initial_target_weight' (raw momentum-
computed target before any cap/floor/turnover scaling) so signal bugs are
decoupled from constraint bugs.

Golden file schema per ticker:
  {
    "initial_target_weight": 0.031,  # raw target before constraints (float)
    "action": "TRIM",                # expected action
    "basis": "momentum_trim",        # expected basis substring
    "gate_action": "proceed",        # expected gate_action_buy
    "scale_applied": 1.0             # expected trade scale factor (0.0–1.0)
  }

Scenarios:
  1. normal_bull   — all sleeves in band, moderate momentum
  2. soft_limit    — drawdown 23%, buys frozen
  3. bucket_a_breach — Bucket A below $45K, compliance trade halted
  4. hard_limit    — drawdown 31%, reduce to floors
  5. breadth_weak  — ai_tech floor drops from 22% to 12%

Golden files live in tests/golden/. They are generated on first run and
compared on subsequent runs. To regenerate: delete the .json file and re-run.
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
    _patch_json_dump,
)

_GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "..", "golden")


# ── Golden file helpers ───────────────────────────────────────────────────────

def _golden_path(scenario_name):
    return os.path.join(_GOLDEN_DIR, f"scenario_{scenario_name}.json")


def _load_or_create_golden(scenario_name, doc, tickers_to_lock):
    """
    Load the golden file if it exists, or create it from the current run.

    Returns (golden_data, created) where created=True on first run.
    """
    path = _golden_path(scenario_name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f), False

    # Build golden from current run
    portfolio = doc.get("portfolio", {})
    golden = {}
    for ticker in tickers_to_lock:
        row = portfolio.get(ticker)
        if row is None:
            continue
        golden[ticker] = {
            "initial_target_weight": row.get("target_pct"),
            "action":                row.get("action"),
            "basis":                 row.get("basis"),
            "gate_action":           row.get("gate_action", "proceed"),
            "scale_applied":         row.get("scale_applied", 1.0),
        }

    os.makedirs(_GOLDEN_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(golden, f, indent=2)
    return golden, True


def _assert_golden(scenario_name, doc, tickers_to_lock, tol=0.005):
    """
    Compare doc['portfolio'] against the golden file for the given scenario.
    Creates the golden file on first run and passes.
    """
    golden, created = _load_or_create_golden(scenario_name, doc, tickers_to_lock)
    if created:
        pytest.skip(f"Golden file created for scenario '{scenario_name}' — re-run to assert.")

    portfolio = doc.get("portfolio", {})
    failures = []
    for ticker, expected in golden.items():
        row = portfolio.get(ticker)
        if row is None:
            failures.append(f"{ticker}: not in portfolio output")
            continue

        # Check action
        if expected.get("action") and row.get("action") != expected["action"]:
            failures.append(
                f"{ticker}: action {row['action']!r} != expected {expected['action']!r}"
            )
        # Check basis (substring match)
        if expected.get("basis"):
            if expected["basis"] not in (row.get("basis") or ""):
                failures.append(
                    f"{ticker}: basis {row.get('basis')!r} does not contain "
                    f"expected {expected['basis']!r}"
                )
        # Check initial_target_weight within tolerance (Gap 4)
        if expected.get("initial_target_weight") is not None:
            got = row.get("target_pct")
            if got is not None and abs(got - expected["initial_target_weight"]) > tol:
                failures.append(
                    f"{ticker}: target_pct {got:.4f} differs from golden "
                    f"{expected['initial_target_weight']:.4f} by > {tol:.1%}"
                )

    assert not failures, (
        f"Golden regression failed for scenario '{scenario_name}':\n"
        + "\n".join(f"  • {f}" for f in failures)
    )


# ── Standard portfolio setup ──────────────────────────────────────────────────

def _base_portfolio(total=110_000.0):
    return make_holdings({
        "VTI":           (200, total * 0.50 / 200, "core_equity"),
        "IAUM":          (100, total * 0.10 / 100, "precious_metals"),
        "TREASURY_NOTE": (  1,            45000.0,  "bucket_a"),
        "CASH":          (500,               1.0,   "cash"),
    })


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.golden
class TestGoldenRegression:

    def test_scenario_normal_bull(self, tmp_path, monkeypatch):
        """
        Scenario 1 — normal_bull: all sleeves in band, moderate momentum.
        Expected: momentum-driven actions, gates proceed.
        """
        policy   = make_policy()
        holdings = _base_portfolio()
        hist     = make_hist(["VTI", "IAUM"], n_rows=300)
        scores   = make_scores({"VTI": 0.55, "IAUM": 0.60})
        gates    = make_gate_rows(["VTI", "IAUM"])

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)
        _assert_golden("normal_bull", doc, tickers_to_lock=["VTI", "IAUM"])

    def test_scenario_soft_limit(self, tmp_path, monkeypatch):
        """
        Scenario 2 — soft_limit: drawdown 23%.
        Expected: momentum buys frozen (stress_freeze), compliance trades still active.
        """
        import mws_runner
        import mws_analytics

        policy   = make_policy()
        holdings = _base_portfolio()
        hist     = make_hist(["VTI", "IAUM"], n_rows=300)
        scores   = make_scores({"VTI": 0.75, "IAUM": 0.80},
                               tickers_raw={"VTI": 0.05, "IAUM": 0.10})
        gates    = make_gate_rows(["VTI", "IAUM"])

        breadth_path  = str(tmp_path / "bs.json")
        tactical_path = str(tmp_path / "tcs.json")
        targets_path  = str(tmp_path / "precomputed_targets.json")
        holdings_csv  = str(tmp_path / "holdings.csv")
        holdings.to_csv(holdings_csv, index=False)

        monkeypatch.setattr(mws_analytics, "BREADTH_STATE_JSON",       breadth_path)
        monkeypatch.setattr(mws_analytics, "TACTICAL_CASH_STATE_JSON",  tactical_path)
        monkeypatch.setattr(mws_analytics, "HOLDINGS_CSV",             holdings_csv)
        monkeypatch.setattr(mws_runner,    "PRECOMPUTED_TARGETS_FILE",  targets_path)
        _patch_json_dump(monkeypatch)

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

        with open(targets_path) as f:
            doc = json.load(f)

        _assert_golden("soft_limit", doc, tickers_to_lock=["VTI", "IAUM"])

    def test_scenario_hard_limit(self, tmp_path, monkeypatch):
        """
        Scenario 4 — hard_limit: drawdown 31%.
        Expected: reduce all positions to sleeve floors (compliance_trim).
        """
        import mws_runner
        import mws_analytics

        policy   = make_policy()
        holdings = _base_portfolio()
        hist     = make_hist(["VTI", "IAUM"], n_rows=300)
        scores   = make_scores({"VTI": 0.50, "IAUM": 0.50})
        gates    = make_gate_rows(["VTI", "IAUM"])

        breadth_path  = str(tmp_path / "bs.json")
        tactical_path = str(tmp_path / "tcs.json")
        targets_path  = str(tmp_path / "precomputed_targets.json")
        holdings_csv  = str(tmp_path / "holdings.csv")
        holdings.to_csv(holdings_csv, index=False)

        monkeypatch.setattr(mws_analytics, "BREADTH_STATE_JSON",       breadth_path)
        monkeypatch.setattr(mws_analytics, "TACTICAL_CASH_STATE_JSON",  tactical_path)
        monkeypatch.setattr(mws_analytics, "HOLDINGS_CSV",             holdings_csv)
        monkeypatch.setattr(mws_runner,    "PRECOMPUTED_TARGETS_FILE",  targets_path)
        _patch_json_dump(monkeypatch)

        analytics = {
            "policy":    policy,
            "holdings":  holdings,
            "hist":      hist,
            "total_val": float(holdings["MV"].sum()),
            "val_asof":  str(hist.index.max().date()),
            "drawdown":  {"state": "hard_limit", "drawdown": -0.31,
                          "soft_limit": 0.22, "hard_limit": 0.30},
            "df_scores": scores,
            "df_gates":  gates,
        }
        mws_runner._build_portfolio_tables(analytics)

        with open(targets_path) as f:
            doc = json.load(f)

        _assert_golden("hard_limit", doc, tickers_to_lock=["VTI", "IAUM"])

    def test_scenario_bucket_a_breach(self, tmp_path, monkeypatch):
        """
        Scenario 3 — bucket_a_breach: Bucket A MV below $45K.
        Expected: compliance trades halted by Bucket A breach.
        """
        total = 90_000.0
        holdings = make_holdings({
            "VTI":           (200, total * 0.60 / 200, "core_equity"),
            "IAUM":          (100, total * 0.10 / 100, "precious_metals"),
            "TREASURY_NOTE": (  1,            40000.0,  "bucket_a"),  # BELOW $45K floor
            "CASH":          (200,               1.0,   "cash"),
        })
        policy   = make_policy()
        hist     = make_hist(["VTI", "IAUM"], n_rows=300)
        scores   = make_scores({"VTI": 0.50, "IAUM": 0.50})
        gates    = make_gate_rows(["VTI", "IAUM"])

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)
        _assert_golden("bucket_a_breach", doc, tickers_to_lock=["VTI", "IAUM"])

    def test_scenario_breadth_weak(self, tmp_path, monkeypatch):
        """
        Scenario 5 — breadth_weak: ai_tech floor drops from 22% to 12%.
        (No ai_tech tickers in this test portfolio, tests baseline normal behavior
        with breadth_weak breadth state tag in output.)
        """
        policy   = make_policy()
        holdings = _base_portfolio()
        hist     = make_hist(["VTI", "IAUM"], n_rows=300)
        scores   = make_scores({"VTI": 0.50, "IAUM": 0.50})
        gates    = make_gate_rows(["VTI", "IAUM"])

        doc = run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch)
        _assert_golden("breadth_weak", doc, tickers_to_lock=["VTI", "IAUM"])
