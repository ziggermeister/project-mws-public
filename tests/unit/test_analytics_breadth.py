"""
tests/unit/test_analytics_breadth.py

Tests for mws_analytics.compute_and_persist_breadth_states().

Tests the breadth-conditioned ai_tech floor (v2.9.6):
  - >=3 positive tickers → effective_floor == 0.22 (strong breadth)
  - <3 positive tickers  → effective_floor == 0.12 (weak breadth)
  - 0 positive tickers   → effective_floor == 0.0  (infeasible)
  - Hysteresis blocks transition until hysteresis_days have elapsed
  - Same-date call is idempotent
"""
import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import matplotlib; matplotlib.use("Agg")
import mws_analytics as mws
from tests.conftest import make_policy as _make_full_policy


# ── Constants derived from canonical policy fixture ───────────────────────────
# These are read from make_policy() so they stay in sync if policy defaults change.

AI_TECH_TICKERS = ["SOXQ", "CHAT", "BOTZ", "DTCR", "GRID"]

_ai_tech_floor = _make_full_policy()["sleeves"]["level2"]["ai_tech"]["floor"]
STRONG_FLOOR  = _ai_tech_floor["strong_breadth_floor"]   # 0.22
WEAK_FLOOR    = _ai_tech_floor["weak_breadth_floor"]     # 0.12
INFEAS_FLOOR  = _ai_tech_floor["infeasible_floor"]       # 0.0
HYST_DAYS     = _ai_tech_floor["breadth_condition"]["hysteresis_days"]  # 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_policy_with_breadth_floor(
    tickers=None,
    strong_floor=STRONG_FLOOR,
    weak_floor=WEAK_FLOOR,
    infeas_floor=INFEAS_FLOOR,
    strong_thresh=3,
    hyst_days=HYST_DAYS,
):
    tickers = tickers or AI_TECH_TICKERS[:]
    return {
        "sleeves": {
            "level2": {
                "ai_tech": {
                    "floor": {
                        "type": "breadth_conditioned",
                        "breadth_condition": {
                            "strong_breadth_threshold": strong_thresh,
                            "hysteresis_days": hyst_days,
                        },
                        "strong_breadth_floor": strong_floor,
                        "weak_breadth_floor":   weak_floor,
                        "infeasible_floor":     infeas_floor,
                        "infeasible_condition": "positive_count == 0",
                    },
                    "cap": 0.32,
                    "tickers": tickers,
                },
                "biotech": {
                    "floor": 0.04,
                    "cap":   0.12,
                    "tickers": ["XBI"],
                },
            }
        },
    }


def _scores_n_positive(n_positive, tickers=None):
    """
    Build df_scores where exactly n_positive of ai_tech tickers have RawScore > 0.
    """
    tickers = tickers or AI_TECH_TICKERS[:]
    rows = []
    for i, t in enumerate(tickers):
        raw = 0.05 if i < n_positive else -0.05
        rows.append({
            "Ticker":   t,
            "Pct":      0.5,
            "RawScore": raw,
        })
    return pd.DataFrame(rows)


def _prime_state(state_path, sleeve, category, pending_category, pending_days, date="2000-01-01"):
    """Write a breadth state JSON file to simulate prior-day state."""
    state = {
        sleeve: {
            "current_category":  category,
            "pending_category":  pending_category,
            "pending_days":      pending_days,
            "last_date":         date,
            "positive_count":    0,
            "floor_exit_count":  0,
            "effective_floor":   STRONG_FLOOR if category == "strong" else WEAK_FLOOR,
        }
    }
    with open(state_path, "w") as f:
        json.dump(state, f)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestComputeAndPersistBreadthStates:

    def test_strong_breadth_gives_22_pct_floor(self, tmp_path):
        """>=3 positive tickers on day 1 → current_category transitions to strong → floor=0.22."""
        state_path = str(tmp_path / "breadth_state.json")
        policy     = _make_policy_with_breadth_floor(hyst_days=1)  # hyst=1 for instant transition
        df         = _scores_n_positive(4)  # 4 of 5 positive

        result = mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)
        # With hyst_days=1, 1 day is enough to transition to strong
        # On first call, pending_days starts at 1, transition fires if pending >= hyst_days
        assert "ai_tech" in result
        assert result["ai_tech"] == pytest.approx(STRONG_FLOOR), (
            f"Expected strong_breadth_floor={STRONG_FLOOR}, got {result['ai_tech']}"
        )

    def test_weak_breadth_gives_12_pct_floor(self, tmp_path):
        """<3 positive tickers on day 1 → weak → floor=0.12 (after hysteresis)."""
        state_path = str(tmp_path / "breadth_state.json")
        policy     = _make_policy_with_breadth_floor(hyst_days=1)
        df         = _scores_n_positive(1)  # only 1 of 5 positive

        result = mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)
        assert "ai_tech" in result
        assert result["ai_tech"] == pytest.approx(WEAK_FLOOR), (
            f"Expected weak_breadth_floor={WEAK_FLOOR}, got {result['ai_tech']}"
        )

    def test_zero_positive_gives_zero_floor(self, tmp_path):
        """0 positive tickers → infeasible → floor=0.0."""
        state_path = str(tmp_path / "breadth_state.json")
        policy     = _make_policy_with_breadth_floor(hyst_days=1)
        df         = _scores_n_positive(0)

        result = mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)
        assert "ai_tech" in result
        assert result["ai_tech"] == pytest.approx(INFEAS_FLOOR)

    def test_hysteresis_blocks_transition_before_hyst_days(self, tmp_path):
        """
        When prior state is 'strong' and today is 'weak', the floor stays at
        strong_floor until pending_days >= hyst_days.
        """
        state_path = str(tmp_path / "breadth_state.json")
        policy     = _make_policy_with_breadth_floor(hyst_days=5)

        # Prime state: currently strong, now going weak for day 1 (pending_days=1)
        _prime_state(state_path, "ai_tech", "strong", "strong", 0, date="2000-01-01")

        # Today: only 1 positive ticker (weak signal), but hyst_days=5 required
        df = _scores_n_positive(1)
        result = mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)

        # After just 1 day of weak signal, current_category remains 'strong'
        # so effective_floor should still be STRONG_FLOOR
        assert result["ai_tech"] == pytest.approx(STRONG_FLOOR), (
            f"Hysteresis should block transition to weak after only 1 day. "
            f"Got floor={result['ai_tech']}"
        )

    def test_transition_fires_at_hyst_days(self, tmp_path):
        """
        When pending_days has reached hyst_days, the transition should fire.
        """
        state_path = str(tmp_path / "breadth_state.json")
        policy     = _make_policy_with_breadth_floor(hyst_days=5)

        # Prime state: currently strong, has been weak for 4 days (one below threshold)
        _prime_state(state_path, "ai_tech", "strong", "weak", 4, date="2000-01-01")

        # Today: still weak → pending_days becomes 5 → transition fires
        df = _scores_n_positive(1)
        result = mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)

        # After 5 days of weak signal, should transition to weak_floor
        assert result["ai_tech"] == pytest.approx(WEAK_FLOOR), (
            f"Expected transition to weak_floor after {HYST_DAYS} days. "
            f"Got floor={result['ai_tech']}"
        )

    def test_same_date_call_is_idempotent(self, tmp_path):
        """Calling twice on the same trading day returns same floor without incrementing counter."""
        state_path = str(tmp_path / "breadth_state.json")
        policy     = _make_policy_with_breadth_floor(hyst_days=3)
        df         = _scores_n_positive(4)

        result1 = mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)
        result2 = mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)

        assert result1 == result2, "Second call on same day should return identical result"

    def test_static_floor_sleeve_not_tracked(self, tmp_path):
        """Sleeves with static (float) floor are not included in the returned dict."""
        state_path = str(tmp_path / "breadth_state.json")
        policy     = _make_policy_with_breadth_floor()
        df         = pd.DataFrame([
            {"Ticker": "XBI", "Pct": 0.5, "RawScore": 0.05},
        ])

        result = mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)
        # biotech has a static floor — should not be in the result
        assert "biotech" not in result, (
            "Static-floor sleeves should not appear in breadth state result"
        )

    def test_state_file_written_correctly(self, tmp_path):
        """The state file must be valid JSON with required keys."""
        state_path = str(tmp_path / "breadth_state.json")
        policy     = _make_policy_with_breadth_floor(hyst_days=1)
        df         = _scores_n_positive(4)

        mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)

        assert os.path.exists(state_path)
        with open(state_path) as f:
            data = json.load(f)

        assert "ai_tech" in data
        entry = data["ai_tech"]
        for key in ("current_category", "pending_category", "pending_days",
                    "last_date", "effective_floor"):
            assert key in entry, f"Missing key '{key}' in breadth state entry"

    def test_no_tmp_file_left_after_successful_write(self, tmp_path):
        """
        Finding 4 regression: atomic write (.tmp + os.replace) must leave no
        .tmp file behind after a successful write.

        A lingering .tmp file indicates the write was not atomic — it means
        os.replace() did not run (e.g., the write raised before reaching it),
        which would also leave the state file in an inconsistent state.
        """
        state_path = str(tmp_path / "breadth_state.json")
        tmp_path_  = state_path + ".tmp"
        policy     = _make_policy_with_breadth_floor(hyst_days=1)
        df         = _scores_n_positive(4)

        mws.compute_and_persist_breadth_states(policy, df, state_path=state_path)

        assert os.path.exists(state_path), "state file was not written"
        assert not os.path.exists(tmp_path_), (
            f"Atomic write left behind a .tmp file at {tmp_path_}. "
            "os.replace() must clean up the .tmp file on success."
        )
