# MWS Backlog

Items logged for future policy review cycles. **Not active constraints. Not part of the execution gate.**

Previously stored in `mws_policy.json → future_review_items` (removed in v2.9.2 — policy files should contain only binding rules).

---

## ewma_regime_shift_vol_clamp
**Status:** `implemented_v2.9.4`
**Source:** ChatGPT fat-tail review (2026-03-11), confirmed by Gemini. Full F1 validation completed 2026-03-11 against extended history (2019–2026).
**Implemented:** 2026-03-11

EWMA volatility lags true volatility during regime transitions, causing z-score gate to oscillate between blindness (vol spike: EWMA slow to rise, threshold widens, gate stops firing) and over-firing (post-crisis: EWMA slow to fall, routine moves score as statistically extreme). Fix: `effective_vol = clamp(ewma_vol, 0.75 × realized_vol_1y, 1.50 × realized_vol_1y)`. Affects all tickers; disproportionate impact on SIVR and IBIT.

**Validation verdict:** PASS. Both bounds well-calibrated across 7 years including COVID (2020) and rate-shock (2022) regimes. Bank of Canada calm-window ceiling concern not empirically confirmed (EWMA/RV1y ratios 0.86–1.11 at onset, below 1.50× ceiling). Ceiling binds mid-COVID (April 2020) to prevent gate lock-up at maximum drawdown. No clamp interaction during 2022 slow grind.

---

## iaum_fat_tail_monitoring
**Status:** `logged_for_future_review`
**Source:** ChatGPT + Gemini fat-tail review (2026-03-11)
**Priority:** Low
**Scope:** Execution gate — IAUM only

IAUM (gold) EWMA/empirical gap at 1.58pp buy-side (emp_p97.5 = 3.86%, EWMA 2σ = 5.44%). Below the 2pp intervention threshold. Sell-gap at 2.97pp — borderline (trigger at 3pp).

**Action:** Monitor `ewma_emp_gap` for IAUM in rebalance run logs. Trigger review if buy-gap > 2pp or sell-gap > 3pp. Run calibration audit after each monthly rebalance until sell-gap stabilises below 2.5pp.

**Current gaps (as of 2026-03-11):**
- Buy-gap: −1.24pp (trigger: 2.0pp)
- Sell-gap: −2.97pp (trigger: 3.0pp) ← borderline

---

## allocation_layer_sleeve_constraint_interaction
**Status:** `logged_for_future_review`
**Source:** ChatGPT Round 4 (2026-03-10), out-of-scope observation
**Priority:** Low
**Scope:** Allocation engine

`percentile_in_band` + sleeve-constraint interaction may create systematic over-weighting of mid-rank assets during regime transitions. When momentum ranks shift sharply, mid-rank tickers within a sleeve at its L1 cap receive proportionally more weight than their updated rank warrants (reallocation bounded by cap). Effect: short-lived over-weight in transitional assets during the rebalance cycle immediately following a regime shift.

**Action:** Investigate in next allocation-engine policy review. Quantify via regime-transition backtests.

---

## vix_floor_in_execution_gate
**Status:** `logged_for_future_review`
**Source:** ChatGPT peer review 2026-03-17 (confirmed concern by Gemini; Gemini considers vol_clamp sufficient)
**Priority:** Medium
**Scope:** Execution gate — vol input

During rapid VIX compression (e.g., 27→23 in a single session, as observed 2026-03-17), EWMA vol may lag reality, causing the 2-day z-score to appear artificially low and the gate to be over-permissive on large intraday moves. Gemini notes the existing vol_clamp (0.75×RV1y floor) partially mitigates this. ChatGPT argues the clamp floor should be augmented with an explicit VIX-derived minimum: `effective_vol = max(EWMA_vol_clamped, VIX/sqrt(252) * sqrt(2))` (approx VIX/11.2%). This would bind during fast vol-crush sessions and preserve gate sensitivity.

**Action:** Backtest vol_clamp + VIX floor vs vol_clamp alone across 2020 COVID crash/recovery, 2022 rate-shock, and 2026 Iran-shock events. Gate should fire ~10–15% more during vol-crush windows. Evaluate F1 (false positive rate on deferred good trades) before adding. Do not implement until backtested.

---

## vxus_core_equity_downside_floor
**Status:** `resolved_v2.9.5`
**Source:** ChatGPT peer review 2026-03-17 (Gemini says execute as-is; split verdict)
**Resolved:** 2026-03-17

With a 0–15% TPV band and 22nd-percentile momentum, VXUS targets only 3.3% TPV — a 60% cut from 8.4% current. ChatGPT proposed a `min_total_soft` floor. Gemini recommended narrowing the band instead.

**Resolution:** VXUS band narrowed to 4%–12% in v2.9.5 (Gemini's approach adopted). `min_total` set to 4%, `max_total` reduced to 12%. Band narrowing addresses over-convexity at low percentiles without introducing a separate soft-floor mechanism. No further action required.

---

## ai_tech_dispersion_aware_floor
**Status:** `logged_for_future_review`
**Source:** ChatGPT peer review 2026-03-17 (Gemini disagrees — says floor is functioning correctly)
**Priority:** Low
**Scope:** Allocation engine — ai_tech sleeve floor behavior

ChatGPT observes that forcing ai_tech to exactly 22% floor ignores intra-sleeve dispersion: in the 2026-03-17 run, GRID was at 83rd percentile while DTCR/BOTZ were at 28th/33rd. The 22% floor treats them as a monolith. Proposed: if ≥1 ai_tech ticker is above 80th percentile, allow sleeve to sit at 24–26% rather than the hard 22% floor.

Gemini counter: the floor prevents total de-allocation during AI rotation; GRID's strength is already expressed through its intra-sleeve weight being maximized at its per-ticker cap. Sleeve-level floor should not expand based on individual ticker signals — that conflates L2 sleeve behavior with L1 momentum policy.

**Action:** Logged as design tension. Do not implement without regime-transition backtest showing the dispersion-aware floor would have improved risk-adjusted return at sector inflection points. Low priority — current floor behavior is consistent with policy intent.

---

## tactical_cash_state_numpy_bool_serialization
**Status:** `implemented_2026-03-21`
**Source:** Discovered during live run validation 2026-03-20 (post-audit)
**Priority:** P1 — state was never persisting on any run; Gemini incorrectly declared false alarm

`compute_and_persist_tactical_cash_state()` in `mws_analytics.py` used `pandas.Series.any()` which returns `numpy.bool_`, not Python `bool`. `json.dump` cannot serialize numpy types. The state file write silently failed on every run. On load, the missing/corrupt file defaulted `filter_blocking=False` and `consecutive_blocked_days=0`, resetting the counter on every run and making the tactical cash state machine non-functional.

Gemini declared this a false alarm in its policy-code consistency audit, stating "Python's json.dump correctly serializes Python True/False". That is true for Python booleans but not for numpy.bool_ — the distinction was missed.

**Fix:** `bool((series).any())` in `mws_analytics.py` line 1565 — wraps the numpy result in a native Python bool before JSON serialization.

---

## sepp_bucket_a_replenishment_rule
**Status:** `logged_for_future_review`
**Source:** Gemini + ChatGPT design review 2026-03-17
**Priority:** High — time-sensitive (December 2026 Treasury maturity)
**Scope:** Policy — Bucket A / SEPP withdrawal pre-positioning
**Review by:** September 2026 (3 months before maturity)

Current Bucket A holds a single Treasury Note maturing December 2026. No formal policy exists for pre-positioning the next $45K SEPP withdrawal (due January 5, 2027). Without a rule, the system will need to liquidate $45K of Bucket B assets at whatever market conditions exist in late December.

**Proposed rule (Gemini):**
- 12 months before withdrawal date: sweep all portfolio yield (dividends, interest) to Bucket A first
- 3 months before: if Bucket A still underfunded, force proportional fractional sells of Bucket B each rebalance until $45K secured

**Action:** Design and peer-review the formal replenishment rule before September 2026. Implement in policy and runner before the December 2026 maturity. Do not wait until November.

---

## urnm_buy_gate_monitoring
**Status:** `logged_for_future_review`
**Source:** Portfolio-wide gate calibration audit 2026-03-11
**Priority:** Low
**Scope:** Execution gate — URNM buy-side only
**Trigger:** `buy_gap_pp > 3.0`

URNM (uranium miners) buy-side EWMA/empirical gap at +1.74pp (emp_p97.5 = 11.19% vs EWMA 2σ gate = 9.45%). Below 3pp recalibration threshold but trending toward fat-tail divergence. Back-solved buy sigma = 2.37 (vs global 2.0). If buy-gap exceeds 3pp: add URNM to `per_ticker_thresholds` with `gate_sigma_buy` override.

**Current gap (as of 2026-03-11):** 1.74pp
**Action:** Include URNM in monthly calibration audit output. No action until `buy_gap_pp > 3pp` for 2 consecutive monthly runs.

---

## momentum_buy_sleeve_cap_headroom
**Status:** `implemented_2026-03-21`
**Source:** Live trade error discovered 2026-03-20 (COPX +45 breach)
**Priority:** P0 — caused a real compliance breach; fixed immediately

`_est_trade()` computed `momentum_buy` size as `abs(target_mv - t_mv)` with no cap on remaining L2 sleeve headroom. When a sleeve was near (but under) its cap, a momentum buy could recommend an amount exceeding headroom, immediately creating a compliance breach upon execution.

**Real impact (2026-03-20):** strategic_materials was at 9.68% (under 10% cap, $2,091 headroom). System recommended COPX +65 sh ($4,465). User executed +45 sh ($3,094) — still $1,003 over headroom. Sleeve moved to 10.15% (ABOVE_CAP). Required 15-share corrective trim across URNM/COPX/REMX on 2026-03-23.

**Fix:** In `_est_trade()` for `momentum_buy`, added:
```python
sleeve_room = max(0.0, cap_frac * denom - l2_total)
est_usd     = min(est_usd, sleeve_room)
```
This clamps the recommended buy to the remaining sleeve capacity, preventing any momentum buy from creating a cap breach. Compliance buys, trims, DEPLOY, and spike-trims are unaffected.

---

## multi_ticker_sleeve_headroom_sharing
**Status:** `implemented_2026-03-21`
**Source:** Gemini 2.5-pro audit 2026-03-20
**Priority:** P0 — sibling to momentum_buy_sleeve_cap_headroom; fixed in same session

When multiple tickers in the same sleeve both receive `momentum_buy` signals, `_est_trade()` was called independently for each, and each saw the full sleeve headroom (`cap_frac * denom - l2_total`). Their combined raw buys could together exceed available headroom, recreating the cap-breach bug for multi-buyer sleeves.

**Example:** precious_metals headroom = $3,242. SIVR wants $2,100 (capped to $3,242 ✓), IAUM wants $2,400 (also capped to $3,242 ✓). Combined = $4,500 > $3,242 → breach on execution.

**Fix:** After the `raw_trades` Pass 1 loop in `_build_portfolio_tables()`, a sleeve-level scaling pass proportionally reduces each ticker's raw USD so the sleeve total equals available headroom:
```python
_sleeve_mom_raw: dict = {}  # l2_name → total raw USD need
for _t, _d in action_items:
    if _d["label"] == "BUY" and "momentum_buy" in _d["basis"]:
        _sleeve_mom_raw[_d["l2"]] = _sleeve_mom_raw.get(_d["l2"], 0.0) + raw_trades[_t][0]
for _l2n, _total_need in _sleeve_mom_raw.items():
    # compute _headroom_s = cap * denom - l2_mv; scale_s = min(1, headroom/need)
    # apply scale_s to all momentum buys in that sleeve
```

---

## hard_limit_turnover_cap_bypass
**Status:** `implemented_2026-03-21`
**Source:** Gemini 2.5-pro audit 2026-03-20
**Priority:** P0 — dormant until ≥30% drawdown; fixed pre-emptively

Policy v2.9.9 explicitly exempts Priority-1 hard_limit compliance trades from the 20% per-event turnover cap, but the code applied `comp_buy_scale = min(1.0, _cash_lim_scale, _turnover_lim_scale)` unconditionally regardless of drawdown state. During a severe drawdown requiring aggressive floor-restoration buys, the turnover cap would clip the exact trades most critical for risk reduction.

**Fix:** In `_build_portfolio_tables()`, check `dd.get("state") == "hard_limit"` before applying the turnover scale:
```python
_in_hard_limit = dd.get("state") == "hard_limit"
comp_buy_scale = (
    min(1.0, _cash_lim_scale)                        # hard_limit: cash-only
    if _in_hard_limit else
    min(1.0, _cash_lim_scale, _turnover_lim_scale)   # normal: cash + turnover cap
)
```

---

## drawdown_key_wrong_policy_field
**Status:** `implemented_2026-03-21`
**Source:** Gemini 2.5-pro re-audit 2026-03-20 (post-fix verification run)
**Priority:** P0 — active on every run; silently using wrong thresholds since policy v2.9.5

`check_drawdown_state()` in `mws_analytics.py` read thresholds from `policy.get("risk_controls", {})`. That key does not exist. Silent fallback to hardcoded defaults: `soft_limit=0.20`, `hard_limit=0.28`. Policy v2.9.5 raised these to 0.22/0.30 (in `drawdown_rules`), but the code never saw those values.

**Impact:** System entered soft_limit (buy freeze) at 20% drawdown instead of 22%, and hard_limit (forced floor-reduction) at 28% instead of 30%. Every run since v2.9.5 (2026-03-17) was using the wrong thresholds.

**Fix:** Changed `policy.get("risk_controls", {})` → `policy.get("drawdown_rules", {})` with correct defaults (0.22/0.30) and recovery threshold from `recovery_condition.drawdown_below` (0.15).

---

## per_ticker_max_total_not_enforced_in_est_trade
**Status:** `implemented_2026-03-21`
**Source:** Gemini 2.5-pro re-audit 2026-03-20
**Priority:** P0 — can cause per-ticker cap breach on any run with an underweight ticker

`_est_trade()` capped buys only against sleeve headroom (L2 cap). It never checked `ticker_constraints[ticker].max_total` — the TPV-based hard limit per ticker (e.g., SOXQ: 10% TPV). A ticker could be within its sleeve cap but above its own max_total cap after a buy.

**Fix:** After computing `est_usd` in `_est_trade()` for `momentum_buy` and `compliance_buy`, added:
```python
_tc = policy.get("ticker_constraints", {}).get(ticker, {})
_max_tot = _tc.get("max_total")
if _max_tot is not None:
    _t_mv        = hold.loc[hold["Ticker"] == ticker, "MV"].sum()
    _ticker_room = max(0.0, float(_max_tot) * total_val - _t_mv)
    est_usd      = min(est_usd, _ticker_room)
```
Same cap also added to the DEPLOY (residual deployment) loop.

---

## momentum_buy_turnover_cap_missing
**Status:** `implemented_2026-03-21`
**Source:** Gemini 2.5-pro re-audit 2026-03-20
**Priority:** P0 — can cause total per-event turnover to exceed 20% cap

`mom_buy_scale` was constrained only by available cash, not by the remaining per-event turnover budget. If compliance buys consumed 15% of the 20% cap, momentum buys could add another 10%, yielding 25% total — above policy limit.

**Fix:** In Phase 2 of the budget waterfall, compute remaining turnover budget and apply it to `mom_buy_scale`:
```python
_turnover_used_comp = comp_buy_need * comp_buy_scale
_remaining_turnover = max(0.0, _turnover_cap_usd - _turnover_used_comp)
_mom_cash_scale     = cash_for_discretionary / mom_buy_need if mom_buy_need > 0 else 1.0
_mom_turnover_scale = _remaining_turnover     / mom_buy_need if mom_buy_need > 0 else 1.0
mom_buy_scale       = min(1.0, _mom_cash_scale, _mom_turnover_scale)
```

---

## drawdown_rolling_252d_window
**Status:** `implemented_2026-03-21`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P0 — active on every run; could keep system stuck in soft/hard_limit indefinitely

`_compute_max_drawdown(wealth)` used `cummax()` over the FULL TWR history (all-time peak). Policy specifies `measurement: peak_to_trough_rolling_252d`. If the portfolio had a deep drawdown years ago, the old all-time high anchors the peak forever and the system could permanently read as hard_limit.

**Fix:** Changed `_compute_max_drawdown(wealth)` → `_compute_max_drawdown(wealth.iloc[-252:])` to measure drawdown from the 1-year rolling high only.

---

## soft_limit_buy_freeze_not_enforced
**Status:** `implemented_2026-03-21`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P1 — momentum buys generated during stress; compliance email incorrect during drawdowns

`_action()` did not check `dd["state"]`. During soft_limit (≥22% drawdown), policy mandates freeze of all new momentum buys. The code generated momentum buy signals regardless, leaving freeze enforcement entirely to the LLM. The compliance email (no LLM) would incorrectly recommend momentum buys during stress.

**Fix:** Added `_dd_state` check in `_action()`: if state is soft_limit or hard_limit and the action would be momentum_buy, return `"HOLD", "hold|stress_freeze"` instead.

---

## gate_direction_hardcoded_buy
**Status:** `implemented_2026-03-21`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P1 — spike-trim and sell-defer both dead code since launch

`run_analytics()` called `check_execution_gate(trade_direction="BUY")` for ALL tickers. Because `spike_trim` fires only on `direction=SELL` and `sell_defer` fires only on `direction=SELL`, both features were completely inactive — dead code paths that never executed despite being described in policy.

**Fix:** Now computes gates for both BUY and SELL directions per ticker, storing `gate_action_buy` and `gate_action_sell` separately. `_action()` uses the appropriate directional gate. Spike-trim now only fires when the ticker is already headed for a TRIM action (not a BUY). Momentum trim sell-defer returns `hold|sell_defer` to block the trim for the cycle.

---

## l1_cap_enforcement_missing
**Status:** `implemented_2026-03-21`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P1 — fixed commit 762f27d

L1 sleeve caps (growth=60%, real_assets=25%, monetary_hedges=15%, speculative=5%) were computed and displayed but never enforced. Combined buys across L2 sleeves within the same L1 could push the L1 total over its cap while each individual L2 was within its own cap.

**Fix:** Added post-scaling L1 cap pass in `_build_portfolio_tables()`. For each L1 sleeve with a cap, compute `_l1_headroom = cap * sizing_denom - current_l1_mv`, sum all BUY raw needs in that L1, and apply `_l1_scale = min(1, headroom/need)` proportionally to all BUY trades in that L1.

---

## per_ticker_min_total_not_enforced
**Status:** `implemented_2026-03-21`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P1 — fixed commit 762f27d

Per-ticker `min_total` (TPV-based floor, e.g. VTI: 10%, VXUS: 4%) was never checked as a compliance trigger. The compliance system only enforced L2 sleeve floors (sizing_denom-based). A ticker could fall below its own TPV floor while its L2 sleeve was at its floor.

**Fix:** Added a pre-check in `_action()` before L2 logic: if `ticker_constraints[ticker].min_total` is defined and `t_mv / total_val < min_total - 0.001` (with band), classify as compliance_buy. Not blocked by stress_freeze (treated as structural floor like L2 compliance).

---

## bucket_a_minimum_not_enforced
**Status:** `implemented_2026-03-21`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P1 — fixed commits 762f27d, b5beb0a, 9ea17da

Bucket A (TREASURY_NOTE ≥ $45K) was computed and flagged as BELOW_MIN but there was no code halting discretionary buys when it was breached.

**Fix (762f27d):** Added `_bucket_a_breach` flag. When breached: `mom_buy_scale = 0.0`, DEPLOY gated, `comp_buy_scale = 0.0` (all buys halted).
**Fix (b5beb0a):** Confirmed compliance buys also correctly gated by `_bucket_a_breach` (Gemini audit found the comp_buy_scale path was initially missed).
**Fix (9ea17da):** Bucket A minimum now read from authoritative policy field `definitions.buckets.bucket_a_protected_liquidity.minimum_usd` (was using nonexistent `bucket_a.minimum_balance`, silently defaulting to $45K).

---

## drawdown_recovery_state_machine_missing
**Status:** `logged_for_future_review`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P1

The recovery condition from the policy (`drawdown < 15% for 10 consecutive days OR VTI positive momentum for 5 consecutive days`) is not implemented. Once the drawdown falls below the trigger threshold, `check_drawdown_state()` will immediately return "normal" (no persistence needed for entry), but the recovery condition adds a confirmation delay. Without it, the system could exit and re-enter stress regime rapidly if drawdown hovers near the threshold.

---

## precomputed_cache_intraday_staleness
**Status:** `logged_for_future_review`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P1 (low urgency — single daily GH Actions run mitigates in practice)

`mws_precomputed_targets.json` freshness keyed on `run_date + holdings_hash`. Same-day changes to prices, breadth state, tactical cash state, or drawdown regime are invisible. Only matters if runner is executed multiple times intraday. Fix: include hash of `mws_ticker_history.csv` latest row, `mws_breadth_state.json`, and `mws_tactical_cash_state.json` in freshness check.

---

## activated_tickers_in_momentum_universe
**Status:** `implemented_2026-03-21`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P2 — fixed commit 762f27d

`run_mws_audit()` included ACTIVATED (not yet inducted) tickers in the ranking universe even though policy marks them `eligible_for_momentum: false`, distorting percentile ranks for inducted names.

**Fix:** Changed `stage in ["INDUCTED", "ACTIVATED"]` → `stage == "INDUCTED"` in `run_mws_audit()` candidate-building loop.

---

## residual_deploy_rule_incorrect
**Status:** `implemented_2026-03-21` (partial — VTI-last rule)
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P2 — fixed commit 762f27d

DEPLOY loop greedily filled any positive-blend HOLD ticker. VTI was not explicitly last as policy requires.

**Fix:** `_deploy_pool` now excludes VTI, sorted by percentile descending, with VTI appended last. Full underweight-first filtering not yet implemented (sleeve cap headroom check approximates this; full implementation deferred as low-urgency).

---

## annual_turnover_not_tracked
**Status:** `logged_for_future_review`
**Source:** OpenAI Codex audit 2026-03-20
**Priority:** P2

Only per-event turnover cap is implemented. Annual YTD turnover (60% cap) and deferred-trade attribution to originating rebalance events are not tracked. Requires a rebalance ledger with rebalance_id and annual YTD accumulator.

---

## fetch_history_fast_exit_skips_universe_changes
**Status:** `implemented_2026-03-21`
**Source:** OpenAI Codex audit 2026-03-20 (also flagged by Gemini)
**Priority:** P2 — fixed commit 762f27d

Fast-exit check verified date freshness but not ticker universe consistency. If policy added or removed tickers, the script exited without backfilling the new name.

**Fix:** Added `_existing_cols` vs `_required_cols` comparison before fast-exit in `mws_fetch_history.py`. Fast-exit only triggers when both the date AND the column set match the required universe. Mismatched universe prints a warning and falls through to a full re-fetch.
