"""
tests/policy/test_policy_contract.py

Contract tests against the real mws_policy.json.

These tests verify structural invariants that must hold for the system to
function correctly. A failure here means the policy file has been modified
in a way that breaks a core system contract — requires human review before
any automated run proceeds.

Also includes TestFixtureMirrorsLivePolicy which asserts that make_policy()
stays in sync with mws_policy.json on the fields that scenario tests depend
on. Drift here causes scenario tests to pass against wrong thresholds.

All contract tests are marked @pytest.mark.contract.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import make_policy as _make_fixture_policy

# Path to the real policy file
POLICY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "mws_policy.json",
)

VALID_STAGES = {"inducted", "activated", "reference", "overlay"}


@pytest.fixture(scope="module")
def policy():
    if not os.path.exists(POLICY_PATH):
        pytest.skip(f"mws_policy.json not found at {POLICY_PATH}")
    with open(POLICY_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── Contract tests ─────────────────────────────────────────────────────────────

@pytest.mark.contract
class TestPolicyContract:

    def test_drawdown_rules_key_exists(self, policy):
        """
        'drawdown_rules' key must exist (Bug #4: was 'risk_controls' — wrong key).
        """
        assert "drawdown_rules" in policy, (
            "Policy must have 'drawdown_rules' key (not 'risk_controls'). "
            "Bug #4 was caused by reading the wrong key."
        )

    def test_drawdown_rules_thresholds_are_floats(self, policy):
        """soft_limit and hard_limit must be numeric."""
        dr = policy["drawdown_rules"]
        assert isinstance(dr["soft_limit"], (int, float))
        assert isinstance(dr["hard_limit"], (int, float))

    def test_soft_limit_less_than_hard_limit(self, policy):
        """soft_limit < hard_limit (logical invariant)."""
        dr = policy["drawdown_rules"]
        assert dr["soft_limit"] < dr["hard_limit"], (
            f"soft_limit ({dr['soft_limit']}) must be < hard_limit ({dr['hard_limit']})"
        )

    def test_bucket_a_minimum_usd_at_correct_path(self, policy):
        """
        Bug #15: Bucket A minimum must be at:
            definitions.buckets.bucket_a_protected_liquidity.minimum_usd
        NOT at bucket_a.minimum_balance (wrong path that caused bug #15).
        """
        try:
            min_usd = (
                policy["definitions"]["buckets"]["bucket_a_protected_liquidity"]["minimum_usd"]
            )
        except KeyError as e:
            pytest.fail(
                f"definitions.buckets.bucket_a_protected_liquidity.minimum_usd missing: {e}. "
                "Bug #15: code reads from this path — if it's missing, Bucket A enforcement fails."
            )
        assert isinstance(min_usd, (int, float)) and min_usd > 0, (
            f"minimum_usd must be a positive number, got {min_usd!r}"
        )

    def test_max_turnover_is_valid_fraction(self, policy):
        """governance.execution.max_turnover must be a float in (0, 1)."""
        mt = policy.get("governance", {}).get("execution", {}).get("max_turnover")
        assert mt is not None, "governance.execution.max_turnover is missing"
        assert isinstance(mt, (int, float)), f"max_turnover must be numeric, got {type(mt)}"
        assert 0 < mt < 1, f"max_turnover must be in (0, 1), got {mt}"

    def test_all_l2_sleeves_have_numeric_floor_and_cap(self, policy):
        """
        All L2 sleeves must have floor and cap.
        Floor can be a float OR a breadth_conditioned dict (ai_tech v2.9.6).
        Cap must be a float.
        """
        sleeves_l2 = policy.get("sleeves", {}).get("level2", {})
        for sleeve_name, sleeve_data in sleeves_l2.items():
            cap = sleeve_data.get("cap")
            assert cap is not None, f"Sleeve '{sleeve_name}' missing 'cap'"
            assert isinstance(cap, (int, float)), (
                f"Sleeve '{sleeve_name}' cap must be numeric, got {type(cap)}"
            )

            floor = sleeve_data.get("floor")
            assert floor is not None, f"Sleeve '{sleeve_name}' missing 'floor'"
            if isinstance(floor, dict):
                # breadth_conditioned floor — check both sub-floors <= cap
                strong = floor.get("strong_breadth_floor", 0)
                weak   = floor.get("weak_breadth_floor", 0)
                assert strong <= cap, (
                    f"Sleeve '{sleeve_name}' strong_breadth_floor ({strong}) > cap ({cap})"
                )
                assert weak <= cap, (
                    f"Sleeve '{sleeve_name}' weak_breadth_floor ({weak}) > cap ({cap})"
                )
            else:
                assert isinstance(floor, (int, float)), (
                    f"Sleeve '{sleeve_name}' floor must be numeric or dict, got {type(floor)}"
                )
                assert floor <= cap, (
                    f"Sleeve '{sleeve_name}' floor ({floor}) > cap ({cap})"
                )

    def test_all_sleeve_tickers_in_ticker_constraints(self, policy):
        """
        Every ticker listed in an L2 sleeve's 'tickers' array must have an entry
        in ticker_constraints (otherwise the system has no lifecycle stage for it).
        """
        sleeves_l2     = policy.get("sleeves", {}).get("level2", {})
        ticker_constraints = policy.get("ticker_constraints", {})
        missing = []
        for sleeve_name, sleeve_data in sleeves_l2.items():
            for t in sleeve_data.get("tickers", []):
                if t not in ticker_constraints:
                    missing.append((t, sleeve_name))
        assert not missing, (
            f"Tickers in sleeves but missing from ticker_constraints: {missing}"
        )

    def test_all_lifecycle_stages_are_valid(self, policy):
        """Every ticker_constraints entry must have a valid lifecycle.stage."""
        tc = policy.get("ticker_constraints", {})
        invalid = []
        for ticker, cfg in tc.items():
            stage = (cfg.get("lifecycle") or {}).get("stage")
            if stage is not None and stage.lower() not in VALID_STAGES:
                invalid.append((ticker, stage))
        assert not invalid, (
            f"Tickers with invalid lifecycle stage: {invalid}. "
            f"Valid stages: {VALID_STAGES}"
        )

    def test_treasury_note_in_fixed_asset_prices(self, policy):
        """TREASURY_NOTE must be in governance.fixed_asset_prices (Bucket A requires it)."""
        fixed = policy.get("governance", {}).get("fixed_asset_prices", {})
        assert "TREASURY_NOTE" in fixed, (
            "TREASURY_NOTE must be in governance.fixed_asset_prices. "
            "Bucket A valuation depends on this entry."
        )

    def test_ai_tech_floor_is_breadth_conditioned_dict(self, policy):
        """
        ai_tech sleeve floor must be a dict with strong_breadth_floor and weak_breadth_floor
        (v2.9.6 breadth-conditioned floor).
        """
        sleeves_l2 = policy.get("sleeves", {}).get("level2", {})
        ai_tech = sleeves_l2.get("ai_tech", {})
        floor   = ai_tech.get("floor")
        assert isinstance(floor, dict), (
            f"ai_tech floor must be a breadth_conditioned dict, got {type(floor)}"
        )
        assert "strong_breadth_floor" in floor, (
            "ai_tech floor dict missing 'strong_breadth_floor' key"
        )
        assert "weak_breadth_floor" in floor, (
            "ai_tech floor dict missing 'weak_breadth_floor' key"
        )
        assert isinstance(floor["strong_breadth_floor"], (int, float))
        assert isinstance(floor["weak_breadth_floor"], (int, float))

    def test_execution_gate_enabled_is_bool(self, policy):
        """execution_gates.short_term_confirmation.enabled must exist and be bool."""
        gate = (
            policy.get("execution_gates", {})
                  .get("short_term_confirmation", {})
        )
        assert "enabled" in gate, (
            "execution_gates.short_term_confirmation.enabled is missing"
        )
        assert isinstance(gate["enabled"], bool), (
            f"execution_gates.short_term_confirmation.enabled must be bool, "
            f"got {type(gate['enabled'])}"
        )

    def test_governance_fixed_asset_prices_cash_is_1(self, policy):
        """CASH must have price 1.0 (it's a stablecoin proxy)."""
        fixed = policy.get("governance", {}).get("fixed_asset_prices", {})
        assert "CASH" in fixed, "CASH missing from governance.fixed_asset_prices"
        cash_price = fixed["CASH"]
        if isinstance(cash_price, dict):
            cash_price = cash_price.get("fallback_price", cash_price)
        assert float(cash_price) == 1.0, f"CASH price must be 1.0, got {cash_price}"

    def test_l2_sleeves_have_tickers_lists(self, policy):
        """Every L2 sleeve must have a 'tickers' list (even if empty)."""
        sleeves_l2 = policy.get("sleeves", {}).get("level2", {})
        for sleeve_name, sleeve_data in sleeves_l2.items():
            assert "tickers" in sleeve_data, (
                f"Sleeve '{sleeve_name}' missing 'tickers' key"
            )
            assert isinstance(sleeve_data["tickers"], list), (
                f"Sleeve '{sleeve_name}'.tickers must be a list"
            )


# ── Fixture/live-policy consistency ──────────────────────────────────────────

@pytest.mark.contract
class TestFixtureMirrorsLivePolicy:
    """
    Assert that make_policy() (the canonical test fixture) stays in sync with
    the real mws_policy.json on the fields that scenario tests depend on.

    If these diverge, scenario tests pass but they are testing the wrong
    thresholds — a silent correctness regression.

    Checked fields:
      - drawdown_rules.soft_limit and hard_limit
      - definitions.buckets.bucket_a_protected_liquidity.minimum_usd
      - governance.execution.max_turnover
      - sleeves.level2.ai_tech.floor.strong_breadth_floor and weak_breadth_floor
      - sleeves.level2.precious_metals.cap
      - sleeves.level2.strategic_materials.cap
      - ticker_constraints.IAUM.max_total
      - ticker_constraints.VTI.min_total and max_total
    """

    @pytest.fixture(scope="class")
    def live(self):
        """Load the real mws_policy.json."""
        if not os.path.exists(POLICY_PATH):
            pytest.skip(f"mws_policy.json not found at {POLICY_PATH}")
        with open(POLICY_PATH, encoding="utf-8") as f:
            return json.load(f)

    @pytest.fixture(scope="class")
    def fixture(self):
        """Return the make_policy() fixture dict."""
        return _make_fixture_policy()

    def _get(self, d, *keys, default=None):
        """Safe nested get."""
        for k in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(k, default)
        return d

    def test_drawdown_soft_limit_matches(self, live, fixture):
        live_val    = self._get(live,    "drawdown_rules", "soft_limit")
        fixture_val = self._get(fixture, "drawdown_rules", "soft_limit")
        assert live_val == fixture_val, (
            f"drawdown_rules.soft_limit: live={live_val}, fixture={fixture_val}. "
            "Update make_policy() to match mws_policy.json."
        )

    def test_drawdown_hard_limit_matches(self, live, fixture):
        live_val    = self._get(live,    "drawdown_rules", "hard_limit")
        fixture_val = self._get(fixture, "drawdown_rules", "hard_limit")
        assert live_val == fixture_val, (
            f"drawdown_rules.hard_limit: live={live_val}, fixture={fixture_val}. "
            "Update make_policy() to match mws_policy.json."
        )

    def test_bucket_a_minimum_usd_matches(self, live, fixture):
        live_val = self._get(
            live, "definitions", "buckets",
            "bucket_a_protected_liquidity", "minimum_usd",
        )
        fixture_val = self._get(
            fixture, "definitions", "buckets",
            "bucket_a_protected_liquidity", "minimum_usd",
        )
        assert live_val == fixture_val, (
            f"bucket_a.minimum_usd: live={live_val}, fixture={fixture_val}. "
            "Update make_policy() to match mws_policy.json."
        )

    def test_max_turnover_matches(self, live, fixture):
        live_val    = self._get(live,    "governance", "execution", "max_turnover")
        fixture_val = self._get(fixture, "governance", "execution", "max_turnover")
        assert live_val == fixture_val, (
            f"governance.execution.max_turnover: live={live_val}, fixture={fixture_val}. "
            "Update make_policy() to match mws_policy.json."
        )

    def test_ai_tech_strong_breadth_floor_matches(self, live, fixture):
        live_val    = self._get(live,    "sleeves", "level2", "ai_tech", "floor", "strong_breadth_floor")
        fixture_val = self._get(fixture, "sleeves", "level2", "ai_tech", "floor", "strong_breadth_floor")
        assert live_val == fixture_val, (
            f"ai_tech.floor.strong_breadth_floor: live={live_val}, fixture={fixture_val}."
        )

    def test_ai_tech_weak_breadth_floor_matches(self, live, fixture):
        live_val    = self._get(live,    "sleeves", "level2", "ai_tech", "floor", "weak_breadth_floor")
        fixture_val = self._get(fixture, "sleeves", "level2", "ai_tech", "floor", "weak_breadth_floor")
        assert live_val == fixture_val, (
            f"ai_tech.floor.weak_breadth_floor: live={live_val}, fixture={fixture_val}."
        )

    def test_precious_metals_cap_matches(self, live, fixture):
        live_val    = self._get(live,    "sleeves", "level2", "precious_metals", "cap")
        fixture_val = self._get(fixture, "sleeves", "level2", "precious_metals", "cap")
        assert live_val == fixture_val, (
            f"precious_metals.cap: live={live_val}, fixture={fixture_val}."
        )

    def test_strategic_materials_cap_matches(self, live, fixture):
        live_val    = self._get(live,    "sleeves", "level2", "strategic_materials", "cap")
        fixture_val = self._get(fixture, "sleeves", "level2", "strategic_materials", "cap")
        assert live_val == fixture_val, (
            f"strategic_materials.cap: live={live_val}, fixture={fixture_val}."
        )

    def test_iaum_max_total_matches(self, live, fixture):
        live_val    = self._get(live,    "ticker_constraints", "IAUM", "max_total")
        fixture_val = self._get(fixture, "ticker_constraints", "IAUM", "max_total")
        assert live_val == fixture_val, (
            f"ticker_constraints.IAUM.max_total: live={live_val}, fixture={fixture_val}."
        )

    def test_vti_min_total_matches(self, live, fixture):
        live_val    = self._get(live,    "ticker_constraints", "VTI", "min_total")
        fixture_val = self._get(fixture, "ticker_constraints", "VTI", "min_total")
        assert live_val == fixture_val, (
            f"ticker_constraints.VTI.min_total: live={live_val}, fixture={fixture_val}."
        )

    def test_vti_max_total_matches(self, live, fixture):
        live_val    = self._get(live,    "ticker_constraints", "VTI", "max_total")
        fixture_val = self._get(fixture, "ticker_constraints", "VTI", "max_total")
        assert live_val == fixture_val, (
            f"ticker_constraints.VTI.max_total: live={live_val}, fixture={fixture_val}."
        )
