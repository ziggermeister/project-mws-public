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
