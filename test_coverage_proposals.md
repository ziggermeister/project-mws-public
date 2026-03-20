# Test Coverage Analysis & Proposals

## Current State

The single test file (`test_mws.py`, 399 lines, 33 test cases) covers only the **mathematical
building blocks** inside `mws_analytics.py`. Every other module — and all high-level orchestration
logic — is untested.

| Module | Functions | Tested | Coverage |
|---|---|---|---|
| `mws_analytics.py` | ~32 public + private | 7 | ~22% |
| `mws_runner.py` | ~16 | 0 | 0% |
| `mws_fetch_history.py` | ~10 | 0 | 0% |
| `mws_charts.py` | ~4 | 0 | 0% |
| `mws_benchmark.py` | ~5 | 0 | 0% |

**Estimated overall function coverage: < 10%**

---

## Priority Areas for Improvement

The proposals below are ordered by risk × testability. Each area lists the specific
gaps, the failure modes that go undetected, and concrete test cases to add.

---

### Area 1 — Policy Helper Functions (Easy wins, zero dependencies)

**Functions:** `get_policy_required_tickers`, `get_ticker_proxy`, `get_ticker_stage`,
`get_ticker_sleeve`

**Why they matter:** These helpers are called in every run — by `run_mws_audit`,
`generate_rankings`, and the runner. A silent bug here (wrong stage classification,
wrong proxy ticker) propagates to rankings and recommendations without any signal.

**Current gap:** Completely untested despite being pure functions with no I/O or
external dependencies.

**Test cases to add:**

```python
class TestGetPolicyRequiredTickers:
    def _make_policy(self):
        return {
            "governance": {
                "reporting_baselines": {
                    "active_benchmarks": ["SPY", "AGG"],
                    "corr_anchor_ticker": "VTI",
                },
                "fixed_asset_prices": {"CASH": 1.0, "TREASURY_NOTE": {"fallback_price": 45000}},
            },
            "ticker_constraints": {
                "VTI":  {"lifecycle": {"stage": "ACTIVATED"}},
                "SOXQ": {"lifecycle": {"stage": "INDUCTED"}},
                "CASH": {},   # synthetic — should be excluded
            },
        }

    def test_includes_benchmarks_and_anchor(self):
        policy = self._make_policy()
        result = mws.get_policy_required_tickers(policy)
        assert "SPY" in result
        assert "AGG" in result
        assert "VTI" in result

    def test_excludes_fixed_price_synthetics(self):
        policy = self._make_policy()
        result = mws.get_policy_required_tickers(policy)
        assert "CASH" not in result
        assert "TREASURY_NOTE" not in result

    def test_includes_real_ticker_constraints(self):
        policy = self._make_policy()
        result = mws.get_policy_required_tickers(policy)
        assert "SOXQ" in result

    def test_empty_policy_returns_empty_set(self):
        assert mws.get_policy_required_tickers({}) == set()


class TestGetTickerProxy:
    def _policy_with_proxy(self, ticker, proxy):
        return {
            "ticker_constraints": {
                ticker: {"lifecycle": {"benchmark_proxy": proxy}}
            },
            "governance": {"reporting_baselines": {"corr_anchor_ticker": "VTI"}},
        }

    def test_returns_explicit_proxy(self):
        policy = self._policy_with_proxy("SOXQ", "SOXX")
        assert mws.get_ticker_proxy(policy, "SOXQ") == "SOXX"

    def test_falls_back_to_corr_anchor(self):
        policy = {"governance": {"reporting_baselines": {"corr_anchor_ticker": "VTI"}},
                  "ticker_constraints": {}}
        assert mws.get_ticker_proxy(policy, "UNKNOWN") == "VTI"

    def test_last_resort_is_vti(self):
        assert mws.get_ticker_proxy({}, "UNKNOWN") == "VTI"

    def test_proxy_normalised_to_upper(self):
        policy = self._policy_with_proxy("ABC", "spy")
        assert mws.get_ticker_proxy(policy, "ABC") == "SPY"


class TestGetTickerStage:
    def _policy(self, stage):
        return {"ticker_constraints": {"VTI": {"lifecycle": {"stage": stage}}}}

    def test_returns_activated(self):
        assert mws.get_ticker_stage(self._policy("ACTIVATED"), "VTI") == "ACTIVATED"

    def test_returns_inducted(self):
        assert mws.get_ticker_stage(self._policy("inducted"), "VTI") == "INDUCTED"

    def test_unknown_ticker_defaults_to_reference(self):
        assert mws.get_ticker_stage({}, "XYZ") == "REFERENCE"


class TestGetTickerSleeve:
    def test_returns_primary_sleeve(self):
        policy = {"ticker_to_sleeves": {"VTI": {"US_CORE": 1.0, "TACTICAL": 0.3}}}
        # max weight sleeve
        assert mws.get_ticker_sleeve(policy, "VTI") == "US_CORE"

    def test_unmapped_ticker(self):
        assert mws.get_ticker_sleeve({}, "XYZ") == "UNMAPPED"

    def test_empty_mapping(self):
        policy = {"ticker_to_sleeves": {"VTI": {}}}
        assert mws.get_ticker_sleeve(policy, "VTI") == "UNMAPPED"
```

---

### Area 2 — `calculate_portfolio_value` (High business risk)

**Why it matters:** This computes the dollar value used in every performance log entry
and every recommendation email. A mis-priced fixed asset or wrong column lookup
silently produces the wrong portfolio value — affecting drawdown calculations and
the LLM's context.

**Current gap:** Zero coverage. The function has two code paths for fixed-asset
pricing (legacy scalar vs. v2.7.1+ structured object) that are completely untested.

**Test cases to add:**

```python
def _make_hist(tickers, prices, dates=None):
    """Build a price history DataFrame (Date index, one column per ticker)."""
    if dates is None:
        dates = pd.bdate_range("2024-01-01", periods=len(prices[0]))
    data = {t: p for t, p in zip(tickers, prices)}
    return pd.DataFrame(data, index=dates)


class TestCalculatePortfolioValue:
    def _policy(self, fixed=None):
        return {"governance": {"fixed_asset_prices": fixed or {}}, "meta": {}}

    def test_basic_market_price_calculation(self):
        hist = _make_hist(["VTI"], [[100.0, 110.0, 120.0]])
        hold = pd.DataFrame({"Ticker": ["VTI"], "Shares": ["10"]})
        policy = self._policy()
        value, _ = mws.calculate_portfolio_value(policy, hold, hist)
        assert math.isclose(value, 1200.0)  # 10 shares × $120 last price

    def test_legacy_scalar_fixed_price(self):
        hist = _make_hist(["VTI"], [[100.0]])
        hold = pd.DataFrame({"Ticker": ["VTI", "CASH"], "Shares": ["10", "500"]})
        policy = self._policy(fixed={"CASH": 1.0})
        value, _ = mws.calculate_portfolio_value(policy, hold, hist)
        assert math.isclose(value, 10 * 100.0 + 500 * 1.0)

    def test_structured_fixed_price_object(self):
        hist = _make_hist(["VTI"], [[100.0]])
        hold = pd.DataFrame({"Ticker": ["TREASURY_NOTE"], "Shares": ["2"]})
        policy = self._policy(fixed={"TREASURY_NOTE": {"price_type": "fixed", "fallback_price": 45000}})
        value, _ = mws.calculate_portfolio_value(policy, hold, hist)
        assert math.isclose(value, 90000.0)

    def test_market_price_type_uses_live_price(self):
        hist = _make_hist(["BND"], [[95.0, 96.0, 97.0]])
        hold = pd.DataFrame({"Ticker": ["BND"], "Shares": ["100"]})
        policy = self._policy(fixed={"BND": {"price_type": "market", "fallback_price": 90.0}})
        value, _ = mws.calculate_portfolio_value(policy, hold, hist)
        assert math.isclose(value, 9700.0)  # 100 × $97 live price

    def test_zero_shares_contributes_zero(self):
        hist = _make_hist(["VTI"], [[100.0]])
        hold = pd.DataFrame({"Ticker": ["VTI"], "Shares": ["0"]})
        policy = self._policy()
        value, _ = mws.calculate_portfolio_value(policy, hold, hist)
        assert math.isclose(value, 0.0)

    def test_missing_holdings_columns_returns_zero(self):
        hist = _make_hist(["VTI"], [[100.0]])
        hold = pd.DataFrame({"Symbol": ["VTI"], "Qty": [10]})  # wrong column names
        policy = self._policy()
        value, _ = mws.calculate_portfolio_value(policy, hold, hist)
        assert math.isclose(value, 0.0)

    def test_asof_date_in_result(self):
        idx = pd.bdate_range("2024-06-01", periods=3)
        hist = pd.DataFrame({"VTI": [100, 101, 102]}, index=idx)
        hold = pd.DataFrame({"Ticker": ["VTI"], "Shares": ["1"]})
        _, asof = mws.calculate_portfolio_value(self._policy(), hold, hist)
        assert asof == "2024-06-05"  # last business day
```

---

### Area 3 — `generate_rankings` (Core product logic)

**Why it matters:** This is the central output of the analytics engine — it ranks
every ticker by momentum and drives the LLM's recommendation. Bugs in percentile
computation, weight override handling, or held/watch status are invisible without tests.

**Current gap:** Zero coverage despite being ~100 lines with branching logic for
weight overrides, missing anchor, empty inputs, and alpha calculation.

**Test cases to add:**

```python
def _make_minimal_policy(tickers, anchor="VTI"):
    """Build a minimal policy dict for rankings tests."""
    tc = {t: {"lifecycle": {"stage": "INDUCTED", "benchmark_proxy": anchor}} for t in tickers}
    tc[anchor] = {"lifecycle": {"stage": "ACTIVATED", "benchmark_proxy": anchor}}
    return {
        "ticker_constraints": tc,
        "ticker_to_sleeves": {},
        "corr_anchor_ticker": anchor,
        "momentum_engine": {"signal_weights": {"tr_12m": 0.45, "slope_6m": 0.35, "residual_3m": 0.20}},
        "governance": {"reporting_baselines": {"alpha_start_date": "2024-01-01"}, "fixed_asset_prices": {}},
    }


def _make_price_hist(tickers_with_returns, n_days=260):
    """Build a price history where each ticker has a specified total return."""
    idx = pd.bdate_range("2023-01-01", periods=n_days)
    data = {}
    for ticker, total_return in tickers_with_returns.items():
        start_price = 100.0
        end_price = start_price * (1 + total_return)
        data[ticker] = np.linspace(start_price, end_price, n_days)
    return pd.DataFrame(data, index=idx)


class TestGenerateRankings:
    def test_empty_candidates_returns_empty_df(self):
        policy = _make_minimal_policy(["VTI"])
        hist = _make_price_hist({"VTI": 0.1})
        hold = pd.DataFrame({"Ticker": [], "Shares": []})
        df = mws.generate_rankings(policy, hist, [], hold)
        assert df.empty

    def test_empty_hist_returns_empty_df(self):
        policy = _make_minimal_policy(["VTI"])
        hold = pd.DataFrame({"Ticker": [], "Shares": []})
        df = mws.generate_rankings(policy, pd.DataFrame(), ["VTI"], hold)
        assert df.empty

    def test_higher_return_ticker_ranked_first(self):
        policy = _make_minimal_policy(["VTI", "SOXQ"])
        hist = _make_price_hist({"VTI": 0.05, "SOXQ": 0.50})  # SOXQ +50%, VTI +5%
        hold = pd.DataFrame({"Ticker": [], "Shares": []})
        df = mws.generate_rankings(policy, hist, ["VTI", "SOXQ"], hold)
        assert df.iloc[0]["Ticker"] == "SOXQ"

    def test_score_is_percentile_rank_0_to_1(self):
        policy = _make_minimal_policy(["VTI", "SOXQ", "AGG"])
        hist = _make_price_hist({"VTI": 0.1, "SOXQ": 0.4, "AGG": -0.1})
        hold = pd.DataFrame({"Ticker": [], "Shares": []})
        df = mws.generate_rankings(policy, hist, ["VTI", "SOXQ", "AGG"], hold)
        assert df["Score"].between(0, 1).all()

    def test_held_ticker_shown_as_held(self):
        policy = _make_minimal_policy(["VTI"])
        hist = _make_price_hist({"VTI": 0.1})
        hold = pd.DataFrame({"Ticker": ["VTI"], "Shares": ["100"]})
        df = mws.generate_rankings(policy, hist, ["VTI"], hold)
        assert "HELD" in df.iloc[0]["Status"]

    def test_unhold_ticker_shown_as_watch(self):
        policy = _make_minimal_policy(["VTI", "SOXQ"])
        hist = _make_price_hist({"VTI": 0.1, "SOXQ": 0.2})
        hold = pd.DataFrame({"Ticker": ["VTI"], "Shares": ["100"]})
        df = mws.generate_rankings(policy, hist, ["VTI", "SOXQ"], hold)
        soxq_row = df[df["Ticker"] == "SOXQ"].iloc[0]
        assert "WATCH" in soxq_row["Status"]

    def test_output_columns_present(self):
        policy = _make_minimal_policy(["VTI"])
        hist = _make_price_hist({"VTI": 0.1})
        hold = pd.DataFrame({"Ticker": [], "Shares": []})
        df = mws.generate_rankings(policy, hist, ["VTI"], hold)
        for col in ["Ticker", "Score", "Pct", "RawScore", "Alpha", "AlphaVs", "Sleeve", "Status"]:
            assert col in df.columns
```

---

### Area 4 — `check_execution_gate` (v2.9.4 trading gate)

**Why it matters:** This function decides whether a trade should proceed, defer, or
spike-trim. A bug here directly causes incorrect trading recommendations. The logic
has multiple branches: gate-disabled, missing history, BUY/SELL sigma thresholds,
stress regime, per-ticker overrides, and vol-clamp behavior.

**Current gap:** Zero coverage for the most complex control-flow function in the codebase.

**Test cases to add:**

```python
def _gate_policy(enabled=True, buy_sigma=2.0, sell_sigma=2.5, max_defer=10,
                 stress_sell_days=3, per_ticker=None, clamp_enabled=False):
    return {
        "execution_gates": {
            "short_term_confirmation": {
                "enabled": enabled,
                "buy_defer_sigma": buy_sigma,
                "sell_defer_sigma": sell_sigma,
                "max_defer_calendar_days": max_defer,
                "ewma_span_days": 126,
                "vol_clamp_enabled": clamp_enabled,
                "stress_regime_overrides": {"sell_defer_max_calendar_days": stress_sell_days},
            },
            "per_ticker_thresholds": per_ticker or {},
        }
    }


def _flat_hist(ticker, n=130, last_spike=0.0):
    """Stable prices with optional large 2-day spike at the end."""
    idx = pd.bdate_range("2023-01-01", periods=n)
    prices = np.ones(n) * 100.0
    if last_spike:
        prices[-1] = 100.0 * (1 + last_spike)
    return pd.DataFrame({ticker: prices}, index=idx)


class TestCheckExecutionGate:
    def test_gate_disabled_always_proceeds(self):
        policy = _gate_policy(enabled=False)
        hist = _flat_hist("VTI")
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        assert result["action"] == "proceed"
        assert result["reason"] == "gate_disabled"

    def test_missing_ticker_history_proceeds(self):
        policy = _gate_policy()
        hist = _flat_hist("VTI")  # SOXQ not in hist
        result = mws.check_execution_gate(policy, "SOXQ", "BUY", hist)
        assert result["action"] == "proceed"
        assert "no_price_history" in result["reason"]

    def test_normal_conditions_proceeds(self):
        policy = _gate_policy(buy_sigma=2.0)
        hist = _flat_hist("VTI", last_spike=0.005)  # 0.5% 2-day return — well below 2σ
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        assert result["action"] == "proceed"

    def test_large_buy_spike_defers(self):
        policy = _gate_policy(buy_sigma=2.0)
        hist = _flat_hist("VTI", last_spike=0.10)  # 10% 2-day spike → z >> 2.0
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        assert result["action"] == "defer"
        assert result["z_score"] is not None
        assert result["z_score"] > 0

    def test_large_sell_spike_triggers_spike_trim(self):
        # On SELL, a large +spike means buy the dip → spike_trim (execute anyway)
        policy = _gate_policy(sell_sigma=2.5)
        hist = _flat_hist("VTI", last_spike=0.12)  # +12% spike, direction=SELL
        result = mws.check_execution_gate(policy, "VTI", "SELL", hist)
        assert result["action"] in {"spike_trim", "proceed"}  # policy-dependent

    def test_stress_regime_collapses_sell_defer_window(self):
        policy = _gate_policy(max_defer=10, stress_sell_days=3)
        hist = _flat_hist("VTI", last_spike=0.10)
        result_normal = mws.check_execution_gate(policy, "VTI", "SELL", hist, stress_active=False)
        result_stress = mws.check_execution_gate(policy, "VTI", "SELL", hist, stress_active=True)
        # Stress collapses max_defer_days from 10 → 3
        assert result_stress["max_defer_days"] <= result_normal["max_defer_days"]

    def test_per_ticker_sigma_override_used(self):
        policy = _gate_policy(
            buy_sigma=2.0,
            per_ticker={"VTI": {"gate_sigma_buy": 99.0}}  # very high → will proceed
        )
        hist = _flat_hist("VTI", last_spike=0.10)
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        assert result["gate_source"] == "per_ticker_override"
        assert result["action"] == "proceed"  # sigma=99 means spike can't breach it

    def test_result_contains_expected_keys(self):
        policy = _gate_policy()
        hist = _flat_hist("VTI")
        result = mws.check_execution_gate(policy, "VTI", "BUY", hist)
        for key in ["action", "reason", "z_score", "threshold", "sigma_used",
                    "gate_source", "max_defer_days", "vol_clamp_type",
                    "raw_vol_2d", "effective_vol_2d"]:
            assert key in result, f"Missing key: {key}"
```

---

### Area 5 — `validate_schema` and `repair_schema` (LLM output defence)

**Why it matters:** These functions are the sole defence against malformed LLM output
reaching the portfolio recommendation email. A regression here could result in
corrupted recommendations being sent or valid responses being falsely rejected.

**Current gap:** Zero coverage despite being pure string-processing functions with
well-defined, testable behaviour.

**Test cases to add:**

```python
import mws_runner as runner


def _wrap(ctx, rec):
    """Wrap content in the two required XML tags."""
    return f"<mws_market_context>{ctx}</mws_market_context><mws_recommendation>{rec}</mws_recommendation>"


class TestValidateSchema:
    def test_clean_response_no_violations(self):
        text = _wrap("Market analysis here.", "Buy VTI.")
        assert runner.validate_schema(text) == []

    def test_missing_market_context_tag(self):
        text = "<mws_recommendation>Buy VTI.</mws_recommendation>"
        violations = runner.validate_schema(text)
        assert any("mws_market_context" in v and "MISSING" in v for v in violations)

    def test_missing_recommendation_tag(self):
        text = "<mws_market_context>Context.</mws_market_context>"
        violations = runner.validate_schema(text)
        assert any("mws_recommendation" in v and "MISSING" in v for v in violations)

    def test_duplicate_tag_detected(self):
        ctx = "<mws_market_context>A</mws_market_context><mws_market_context>B</mws_market_context>"
        rec = "<mws_recommendation>R</mws_recommendation>"
        violations = runner.validate_schema(ctx + rec)
        assert any("2 times" in v or "appears" in v for v in violations)

    def test_text_outside_tags_detected(self):
        text = "Preamble text. " + _wrap("Context.", "Rec.")
        violations = runner.validate_schema(text)
        assert any("outside" in v.lower() for v in violations)

    def test_oversized_response_detected(self):
        big = "x" * (runner.MAX_RESPONSE_CHARS + 1)
        violations = runner.validate_schema(big)
        assert any("MAX_RESPONSE_CHARS" in v for v in violations)

    def test_case_insensitive_tag_matching(self):
        text = "<MWS_MARKET_CONTEXT>ctx</MWS_MARKET_CONTEXT><MWS_RECOMMENDATION>rec</MWS_RECOMMENDATION>"
        assert runner.validate_schema(text) == []


class TestRepairSchema:
    def test_alias_market_context_normalised(self):
        text = "<market_context>ctx</market_context><mws_recommendation>rec</mws_recommendation>"
        repaired, repairs = runner.repair_schema(text)
        assert "<mws_market_context>" in repaired
        assert len(repairs) > 0

    def test_alias_recommendation_normalised(self):
        text = "<mws_market_context>ctx</mws_market_context><recommendation>rec</recommendation>"
        repaired, repairs = runner.repair_schema(text)
        assert "<mws_recommendation>" in repaired

    def test_preamble_stripped(self):
        text = "Some preamble. " + _wrap("ctx", "rec")
        repaired, repairs = runner.repair_schema(text)
        assert not repaired.startswith("Some")
        assert any("preamble" in r.lower() for r in repairs)

    def test_postamble_stripped(self):
        text = _wrap("ctx", "rec") + " Trailing text."
        repaired, repairs = runner.repair_schema(text)
        assert "Trailing" not in repaired

    def test_clean_input_unchanged(self):
        text = _wrap("ctx", "rec")
        repaired, repairs = runner.repair_schema(text)
        assert repairs == []
        assert repaired == text
```

---

### Area 6 — `compute_alpha_vs_proxy` (Alpha calculation)

**Why it matters:** Alpha vs. proxy is displayed in every ranking table and emailed
to the portfolio owner. An off-by-one on the start date or a missing-ticker silent
fallback produces a wrong alpha figure that shapes the LLM recommendation.

**Current gap:** Untested pure function.

**Test cases to add:**

```python
class TestComputeAlphaVsProxy:
    def _make_hist_aligned(self, ticker_return, proxy_return, n=100):
        idx = pd.bdate_range("2024-01-01", periods=n)
        return pd.DataFrame({
            "SOXQ": np.linspace(100, 100 * (1 + ticker_return), n),
            "VTI":  np.linspace(100, 100 * (1 + proxy_return),  n),
        }, index=idx)

    def test_positive_alpha_when_ticker_outperforms(self):
        hist = self._make_hist_aligned(0.30, 0.10)
        alpha = mws.compute_alpha_vs_proxy(hist, "SOXQ", "VTI", pd.Timestamp("2024-01-01"))
        assert alpha is not None
        assert alpha > 0

    def test_negative_alpha_when_ticker_underperforms(self):
        hist = self._make_hist_aligned(0.05, 0.25)
        alpha = mws.compute_alpha_vs_proxy(hist, "SOXQ", "VTI", pd.Timestamp("2024-01-01"))
        assert alpha is not None
        assert alpha < 0

    def test_zero_alpha_when_same_return(self):
        hist = self._make_hist_aligned(0.15, 0.15)
        alpha = mws.compute_alpha_vs_proxy(hist, "SOXQ", "VTI", pd.Timestamp("2024-01-01"))
        assert alpha is not None
        assert math.isclose(alpha, 0.0, abs_tol=1e-6)

    def test_missing_ticker_returns_none(self):
        hist = self._make_hist_aligned(0.10, 0.10)
        alpha = mws.compute_alpha_vs_proxy(hist, "MISSING", "VTI", pd.Timestamp("2024-01-01"))
        assert alpha is None

    def test_missing_proxy_returns_none(self):
        hist = self._make_hist_aligned(0.10, 0.10)
        alpha = mws.compute_alpha_vs_proxy(hist, "SOXQ", "MISSING", pd.Timestamp("2024-01-01"))
        assert alpha is None

    def test_start_date_filters_history(self):
        # All data is from 2024-01-01; start_date of 2025-01-01 → no data → None
        hist = self._make_hist_aligned(0.20, 0.10, n=50)
        alpha = mws.compute_alpha_vs_proxy(hist, "SOXQ", "VTI", pd.Timestamp("2030-01-01"))
        assert alpha is None
```

---

## Summary of Recommended Test Additions

| Area | New Test Classes | Est. Test Cases | Effort |
|---|---|---|---|
| Policy helpers (get_policy_required_tickers, proxy, stage, sleeve) | 4 | ~18 | Low |
| `calculate_portfolio_value` | 1 | ~7 | Low |
| `generate_rankings` | 1 | ~7 | Medium |
| `check_execution_gate` | 1 | ~8 | Medium |
| `validate_schema` + `repair_schema` | 2 | ~12 | Low |
| `compute_alpha_vs_proxy` | 1 | ~6 | Low |
| **Total** | **10** | **~58** | ~1 day |

Implementing these proposals would raise estimated function coverage from **<10% to ~40%**
and — more importantly — would cover the **entire critical path** from policy loading
through ranking, gate evaluation, and output validation.

---

## Further Work (Backlog)

These areas require more effort (mocking, temp files, monkeypatching) and should be
addressed after the above:

- **`run_mws_audit`** — Test candidate selection logic, `missing_from_hist` list, held-ticker
  inclusion
- **`update_performance_log`** — TWR computation, SEPP cash flow handling, CSV persistence;
  requires temp-file fixtures
- **`mws_runner.build_prompt`** — Prompt structure and section ordering; pure string function
- **`mws_fetch_history.fetch_ticker`** — Mock `requests.get` to test retry logic, Stooq
  format conversion, rate limiting
- **Integration test** — End-to-end: `load_system_files` → `run_mws_audit` → `generate_rankings`
  → `check_execution_gate` using fixture CSV files; validates the full pipeline with no I/O
  to external services
- **Add `pytest-cov`** — Configure `pytest --cov=mws_analytics --cov=mws_runner --cov-report=html`
  to measure coverage objectively and enforce a minimum threshold in CI
