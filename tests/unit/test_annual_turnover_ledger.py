"""
tests/unit/test_annual_turnover_ledger.py

Tests for the annual turnover ledger (ITEM 5: annual_turnover_not_tracked).

Covers:
  - load_rebalance_ledger(): empty file, year pruning, ytd accumulation
  - append_rebalance_event(): file creation, idempotency, tmp cleanup
  - mws_runner._build_portfolio_tables(): ytd_traded_usd in trade_budget,
    annual cap enforcement, hard_limit exemption
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import matplotlib; matplotlib.use("Agg")
import mws_analytics as mws


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ledger_path(tmp_path, name="ledger.json"):
    return str(tmp_path / name)


# ── Tests: load_rebalance_ledger ───────────────────────────────────────────────

class TestLoadRebalanceLedger:

    def test_load_empty_ledger_returns_defaults(self, tmp_path):
        """No file → load_rebalance_ledger() returns {'events': [], 'ytd_traded_usd': 0.0}."""
        path = _ledger_path(tmp_path)
        result = mws.load_rebalance_ledger(ledger_path=path)
        assert result == {"events": [], "ytd_traded_usd": 0.0}, (
            "Missing ledger file must return empty defaults"
        )

    def test_append_event_creates_file(self, tmp_path, monkeypatch):
        """append_rebalance_event creates the ledger file with correct fields."""
        path = _ledger_path(tmp_path)
        monkeypatch.setattr(mws, "_todays_trading_date", lambda: "2026-03-21")
        mws.append_rebalance_event(5000.0, 200_000.0, ledger_path=path)

        assert os.path.exists(path), "Ledger file must be created"
        with open(path) as f:
            doc = json.load(f)
        assert len(doc["events"]) == 1
        ev = doc["events"][0]
        assert ev["date"] == "2026-03-21"
        assert ev["traded_usd"] == 5000.0
        assert ev["tpv_at_event"] == 200_000.0
        assert ev["turnover_pct"] == 2.5  # 5000/200000*100

    def test_ytd_accumulates_across_events(self, tmp_path, monkeypatch):
        """Appending two events on different dates: ytd_traded_usd = sum of both."""
        path = _ledger_path(tmp_path)
        monkeypatch.setattr(mws, "_todays_trading_date", lambda: "2026-01-15")
        mws.append_rebalance_event(10_000.0, 200_000.0, ledger_path=path)

        monkeypatch.setattr(mws, "_todays_trading_date", lambda: "2026-02-20")
        mws.append_rebalance_event(8_000.0, 200_000.0, ledger_path=path)

        ledger = mws.load_rebalance_ledger(ledger_path=path)
        assert ledger["ytd_traded_usd"] == 18_000.0, (
            "YTD total must accumulate across multiple events"
        )
        assert len(ledger["events"]) == 2

    def test_prior_year_events_pruned(self, tmp_path):
        """load_rebalance_ledger() must prune events from prior calendar years."""
        import datetime
        current_year = str(datetime.datetime.today().year)
        prior_year   = str(datetime.datetime.today().year - 1)

        path = _ledger_path(tmp_path)
        raw = {
            "events": [
                {"date": f"{prior_year}-12-31", "traded_usd": 50_000.0, "tpv_at_event": 200_000.0, "turnover_pct": 25.0},
                {"date": f"{current_year}-03-01", "traded_usd": 10_000.0, "tpv_at_event": 200_000.0, "turnover_pct": 5.0},
            ],
            "ytd_traded_usd": 60_000.0,
        }
        with open(path, "w") as f:
            json.dump(raw, f)

        ledger = mws.load_rebalance_ledger(ledger_path=path)
        assert len(ledger["events"]) == 1, "Prior-year event must be pruned"
        assert ledger["events"][0]["date"].startswith(current_year)
        assert ledger["ytd_traded_usd"] == 10_000.0, "ytd_traded_usd must reflect only current-year events"

    def test_idempotent_same_day(self, tmp_path, monkeypatch):
        """Appending twice on same date → ledger has only one entry."""
        path = _ledger_path(tmp_path)
        monkeypatch.setattr(mws, "_todays_trading_date", lambda: "2026-03-21")

        mws.append_rebalance_event(5000.0, 200_000.0, ledger_path=path)
        mws.append_rebalance_event(9999.0, 200_000.0, ledger_path=path)  # second call same day

        ledger = mws.load_rebalance_ledger(ledger_path=path)
        assert len(ledger["events"]) == 1, "Idempotency: same-day append must not create duplicate"
        assert ledger["events"][0]["traded_usd"] == 5000.0, "First event value must be preserved"

    def test_no_tmp_file_left_after_write(self, tmp_path, monkeypatch):
        """append_rebalance_event must clean up .tmp file after atomic write."""
        path = _ledger_path(tmp_path)
        monkeypatch.setattr(mws, "_todays_trading_date", lambda: "2026-03-21")
        mws.append_rebalance_event(5000.0, 200_000.0, ledger_path=path)

        assert not os.path.exists(path + ".tmp"), (
            "Atomic write must not leave a .tmp file behind"
        )


# ── Tests: runner integration ─────────────────────────────────────────────────

class TestAnnualTurnoverInRunner:
    """
    Tests for YTD field injection and annual cap enforcement in
    _build_portfolio_tables() (mws_runner.py).
    """

    def _run_tables(self, tmp_path, monkeypatch, policy=None, holdings=None,
                    hist=None, scores=None, gates=None, drawdown_state="normal",
                    drawdown_val=0.0, ledger_data=None):
        """Helper: set up and run _build_portfolio_tables(), return parsed JSON."""
        import mws_runner
        import mws_analytics as _mws
        from tests.conftest import (
            make_policy, make_hist, make_holdings, make_scores, make_gate_rows
        )

        if policy is None:
            policy = make_policy()
        ba_min = policy["definitions"]["buckets"]["bucket_a_protected_liquidity"]["minimum_usd"]

        if holdings is None:
            total = 200_000.0
            holdings = make_holdings({
                "VTI":           (400, total * 0.60 / 400, "core_equity"),
                "IAUM":          ( 50, total * 0.04 / 50,  "precious_metals"),
                "TREASURY_NOTE": (  1, float(ba_min),       "bucket_a"),
                "CASH":          (max(1, round(total - total * 0.64 - ba_min)), 1.0, "cash"),
            })
        if hist is None:
            hist = make_hist(["VTI", "IAUM"], n_rows=300)
        if scores is None:
            scores = make_scores({"VTI": 0.5, "IAUM": 0.5})
        if gates is None:
            gates = make_gate_rows(["VTI", "IAUM"])

        targets_path  = str(tmp_path / "precomputed_targets.json")
        holdings_csv  = str(tmp_path / "holdings.csv")
        ledger_path   = str(tmp_path / "rebalance_ledger.json")
        holdings.to_csv(holdings_csv, index=False)

        # Optionally pre-seed ledger
        if ledger_data is not None:
            with open(ledger_path, "w") as f:
                json.dump(ledger_data, f)

        monkeypatch.setattr(_mws,       "HOLDINGS_CSV",             holdings_csv)
        monkeypatch.setattr(_mws,       "BREADTH_STATE_JSON",       str(tmp_path / "bs.json"))
        monkeypatch.setattr(_mws,       "TACTICAL_CASH_STATE_JSON", str(tmp_path / "tcs.json"))
        monkeypatch.setattr(_mws,       "REBALANCE_LEDGER_JSON",    ledger_path)
        monkeypatch.setattr(mws_runner, "PRECOMPUTED_TARGETS_FILE", targets_path)

        total_val = float(holdings["MV"].sum())
        analytics = {
            "policy":    policy,
            "holdings":  holdings,
            "hist":      hist,
            "total_val": total_val,
            "val_asof":  str(hist.index.max().date()),
            "drawdown":  {
                "state":      drawdown_state,
                "drawdown":   drawdown_val,
                "soft_limit": policy["drawdown_rules"]["soft_limit"],
                "hard_limit": policy["drawdown_rules"]["hard_limit"],
            },
            "df_scores": scores,
            "df_gates":  gates,
        }
        mws_runner._build_portfolio_tables(analytics)

        with open(targets_path) as f:
            return json.load(f)

    def test_ytd_fields_in_precomputed_targets(self, tmp_path, monkeypatch):
        """trade_budget in precomputed_targets.json must contain ytd_traded_usd key."""
        doc = self._run_tables(tmp_path, monkeypatch)
        assert "ytd_traded_usd" in doc["trade_budget"], (
            "precomputed_targets.json trade_budget must contain 'ytd_traded_usd'"
        )
        assert "ytd_turnover_pct" in doc["trade_budget"]
        assert "ytd_remaining_usd" in doc["trade_budget"]
        assert "annual_cap_pct" in doc["trade_budget"]

    def test_annual_cap_enforced_in_runner(self, tmp_path, monkeypatch):
        """
        Pre-seed ledger with ytd_used near annual cap → turnover_cap_usd capped to ytd_remaining.
        """
        import datetime
        current_year = str(datetime.datetime.today().year)

        total = 200_000.0
        # Annual cap = 60% of 200K = 120K. Pre-seed 110K used → 10K remaining.
        ytd_used = 110_000.0
        ledger_data = {
            "events": [
                {"date": f"{current_year}-01-15", "traded_usd": ytd_used,
                 "tpv_at_event": total, "turnover_pct": 55.0},
            ],
            "ytd_traded_usd": ytd_used,
        }

        doc = self._run_tables(tmp_path, monkeypatch, ledger_data=ledger_data)
        tb = doc["trade_budget"]

        annual_cap_usd  = total * 0.60   # 120_000
        ytd_remaining   = annual_cap_usd - ytd_used  # 10_000
        event_cap_usd   = total * 0.20   # 40_000 (normal per-event cap)

        assert tb["ytd_traded_usd"] == ytd_used, "ytd_traded_usd must reflect pre-seeded ledger"
        assert tb["turnover_cap_usd"] <= ytd_remaining + 1.0, (
            f"Annual cap must bind: turnover_cap_usd ({tb['turnover_cap_usd']}) "
            f"must be ≤ ytd_remaining ({ytd_remaining}), not the full event cap ({event_cap_usd})"
        )

    def test_hard_limit_exempt_from_annual_cap(self, tmp_path, monkeypatch):
        """
        In hard_limit, annual cap does NOT reduce turnover_cap_usd
        (hard_limit compliance trades are exempt from all turnover caps).
        """
        import datetime
        current_year = str(datetime.datetime.today().year)
        from tests.conftest import make_policy, make_hist, make_holdings, make_scores, make_gate_rows

        total = 200_000.0
        # Exhaust annual cap completely (ytd_used = 120K → ytd_remaining = 0)
        ytd_used = 120_000.0
        ledger_data = {
            "events": [
                {"date": f"{current_year}-01-15", "traded_usd": ytd_used,
                 "tpv_at_event": total, "turnover_pct": 60.0},
            ],
            "ytd_traded_usd": ytd_used,
        }

        policy = make_policy()
        ba_min = policy["definitions"]["buckets"]["bucket_a_protected_liquidity"]["minimum_usd"]
        # Put IAUM well below floor to force compliance buy
        holdings = make_holdings({
            "VTI":           (400, total * 0.60 / 400, "core_equity"),
            "IAUM":          (  1, 1.0,                "precious_metals"),  # far below floor
            "TREASURY_NOTE": (  1, float(ba_min),       "bucket_a"),
            "CASH":          (max(1, round(total - total * 0.60 - ba_min - 1)), 1.0, "cash"),
        })

        doc = self._run_tables(
            tmp_path, monkeypatch,
            policy=policy, holdings=holdings,
            drawdown_state="hard_limit", drawdown_val=-0.32,
            ledger_data=ledger_data,
        )
        tb = doc["trade_budget"]

        # In hard_limit, the annual cap must NOT zero out the event budget.
        # The turnover_cap_usd should NOT be capped to 0 (ytd_remaining = 0)
        # because hard_limit compliance buys are exempt.
        # We verify that the precomputed targets were written (runner didn't crash)
        # and annual_cap_pct reflects the policy.
        assert tb["annual_cap_pct"] == 60.0, "annual_cap_pct must be 60.0"
        # Hard limit: turnover_cap_usd is NOT reduced to ytd_remaining (which is 0)
        # The code skips the annual cap reduction when _in_hard_limit is True.
        # If the exemption works, turnover_cap_usd > 0 (not zeroed by annual cap).
        assert tb["turnover_cap_usd"] > 0, (
            "hard_limit compliance trades must be exempt from annual cap — "
            "turnover_cap_usd must remain positive even when ytd_remaining=0"
        )
