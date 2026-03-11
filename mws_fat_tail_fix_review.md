# MWS Execution Gate — Fat-Tail Fix Review
**Policy:** MWS v2.9.2 → proposed v2.9.3
**Date:** 2026-03-10
**Scope:** Targeted fix for SIVR and IBIT execution gate thresholds. No changes to momentum signal, blend weights, sleeve caps/floors, or any other gate parameters.
**Prior context:** Execution gate finalized in v2.9.2 after 4-round LLM review (ChatGPT + Gemini). Fat-tail issue was explicitly flagged as `fat_tail_monitor` and deferred. This brief revisits it with 3 fix options and one newly identified structural concern.

---

## 1. Problem Restatement

The v2.9.2 execution gate uses an EWMA-derived 2σ threshold per ticker:

```
2d_vol = annual_vol / sqrt(126)
buy_gate  = 2d_vol × buy_defer_sigma   (2.0σ)
sell_gate = 2d_vol × sell_defer_sigma  (2.5σ)
```

For most tickers, this aligns closely with the empirical 97.5th/2.5th percentile of actual 2-day returns. For two tickers, it does not:

| Ticker | 2d Vol | Buy Gate (EWMA 2σ) | Emp p97.5 | Buy Gap | Sell Gate (EWMA 2.5σ) | Emp p2.5 | Sell Gap |
|--------|--------|-------------------|-----------|---------|----------------------|----------|----------|
| SIVR   | 7.09%  | **14.17%**        | 8.58%     | +5.59pp | **17.71%**           | −6.89%   | +10.82pp |
| IBIT   | 4.62%  | **9.24%**         | 6.88%     | +2.36pp | **11.55%**           | −7.11%   | +4.44pp  |

**Why this matters:** The gate currently protects against buying spikes better than selling into crashes. For SIVR the sell-defer threshold is −17.71% while the empirical crash percentile is −6.89%. The intended capitulation protection almost never activates.

More precisely:
- SIVR buy-defer fires at the **99.7th percentile** of its actual return distribution, not the intended 97.5th.
- SIVR sell-defer fires at the **99.5th percentile** of the downside distribution — almost never.
- IBIT buy-defer fires at roughly the **98.5th percentile**.

The v2.9.2 decision was `keep_ewma_uniform` with monitoring. This review asks whether a targeted fix is warranted.

---

## 2. Scope

Fix applies **only to SIVR and IBIT**. All other tickers have EWMA/empirical gaps below 2pp and the gate fires as intended. IAUM has a 1.58pp gap — just below a reasonable intervention threshold — and is not included in any option below. Reviewers are asked whether IAUM should be included (see Q2).

No fix changes the gate logic, the sigma values for other tickers, the momentum signal, or any allocation engine parameters.

---

## 3. Newly Identified Structural Concern: EWMA Lag During Regime Shifts

**This concern is independent of the fat-tail issue and applies system-wide. It is presented here because it interacts with all three fix options and should inform the reviewer's recommendation.**

The gate z-score is `ret_2d / ewma_vol_2d`. EWMA volatility lags true volatility during regime transitions, producing two failure modes:

**Failure mode A — Vol spike → gate blindness (more dangerous):**
EWMA vol is slow to catch a sudden vol increase. During the first days of a shock, a large return is divided by a still-low EWMA vol, producing an inflated z-score and causing the gate to over-fire (excessive deferrals). Once EWMA catches up — typically 2–4 weeks later — the gate goes blind: the same large move now produces z ≈ 1σ and the gate stops firing exactly when the market is most volatile.

Example (SIVR, shock vol = 12% vs. normal 7%):
```
First week of shock:   ret_2d = +8%, EWMA_vol_2d = 7%  → z = 1.14σ  (gate under-fires, EWMA not yet elevated)
Three weeks later:     ret_2d = +8%, EWMA_vol_2d = 11% → z = 0.73σ  (gate blind, EWMA now elevated)
Gate threshold widens from 14.17% → ~24% during shock — almost nothing triggers it.
```

**Failure mode B — Vol compression → gate over-fires:**
After a crisis, true vol drops faster than EWMA decays. If EWMA overshoots below true vol (possible with the 126-day span), routine moves produce inflated z-scores, causing excessive buy- and sell-deferrals during calm markets.

**Why SIVR and IBIT are disproportionately affected:**
- Vol regimes shift faster for silver and crypto (vol clustering is more pronounced)
- Fat-tailed assets produce large returns without proportional vol change, amplifying EWMA distortion
- Their already-wide EWMA gates widen further during a vol spike, making gate blindness near-total

**Interaction with the fat-tail fixes:**
- Fix 1 (sigma override): retains EWMA vol in the denominator — regime-shift distortion remains
- Fix 2 (empirical percentile): does not use vol in the denominator — partially immune to regime shifts, but rolling windows lag structural changes
- Fix 1b (below): addresses regime-shift distortion directly via a vol clamp

**GPT assessment:** "This is not catastrophic, but it reduces the consistency of the protection you designed." Recommended adding a brief acknowledgment note rather than a full redesign.

---

## 4. Fix Options

### Fix 1 — Per-ticker `gate_sigma` override (Recommended baseline)

**Mechanism:** Add `gate_sigma_buy` and `gate_sigma_sell` fields to `per_ticker_thresholds` for SIVR and IBIT. Gate threshold becomes `vol_2d × gate_sigma` instead of `vol_2d × global_sigma`. All other tickers unchanged.

**Calibration — back-solve from empirical p97.5:**
```
gate_sigma = emp_p975 / vol_2d

SIVR buy:  0.0858 / 0.0709 = 1.21σ  → gate_sigma_buy  = 1.25 (rounded up)
SIVR sell: 0.0689 / 0.0709 = 0.97σ  → gate_sigma_sell = 1.0  (see Q3 — may be too sensitive)
IBIT buy:  0.0688 / 0.0462 = 1.49σ  → gate_sigma_buy  = 1.5
IBIT sell: 0.0711 / 0.0462 = 1.54σ  → gate_sigma_sell = 1.5
```

**Policy JSON change:**
```json
"SIVR": {
  "vol_ann": 0.7955, "vol_2d": 0.0709,
  "buy_gate": 0.0886, "sell_gate": 0.0709,
  "gate_sigma_buy": 1.25, "gate_sigma_sell": 1.0,
  "emp_p975": 0.0858, "ewma_emp_gap": 0.0559,
  "flag": "fat_tail_monitor",
  "fix_method": "per_ticker_gate_sigma_override"
},
"IBIT": {
  "vol_ann": 0.5186, "vol_2d": 0.0462,
  "buy_gate": 0.0693, "sell_gate": 0.0693,
  "gate_sigma_buy": 1.5, "gate_sigma_sell": 1.5,
  "emp_p975": 0.0688, "ewma_emp_gap": 0.0236,
  "flag": "fat_tail_monitor",
  "fix_method": "per_ticker_gate_sigma_override"
}
```

**Runner change:** ~2 lines. Check for `gate_sigma_buy`/`gate_sigma_sell` in the per-ticker entry; fall back to global sigma if absent.

**Pros:** Zero new computation. Uses existing EWMA vol. Fully policy-driven. Adaptive: if vol regime shifts, threshold recomputes from new `vol_2d` scaled by the calibrated sigma.

**Cons:** `gate_sigma` values calibrated from `emp_p975` as of 2026-03-10. Fat-tail shape may change. Annual recalibration needed. Does not address regime-shift distortion (Section 3).

---

### Fix 1b — Per-ticker `gate_sigma` override + EWMA vol clamp (Recommended if regime-shift concern is accepted)

**Mechanism:** Fix 1 plus a volatility floor and ceiling on the EWMA vol used in the z-score denominator. Prevents the gate from going blind during vol spikes or over-firing after vol compression.

**Vol clamp:**
```
effective_vol_2d = clamp(ewma_vol_2d,
                         0.75 × realized_vol_2d_1y,
                         1.50 × realized_vol_2d_1y)

z = ret_2d / effective_vol_2d
```

Where `realized_vol_2d_1y` = trailing 252-day standard deviation of 2-day returns (already available from `mws_ticker_history.csv`).

**What the clamp does:**
- Floor (0.75×): prevents EWMA from collapsing below 75% of 1-year realized vol → prevents over-firing after vol compression
- Ceiling (1.50×): prevents EWMA from spiking above 150% of 1-year realized vol → prevents gate blindness during vol spikes

**Runner change:** ~3 additional lines (compute realized_vol_2d_1y, apply clamp). Applied system-wide — all tickers benefit.

**Policy JSON change:** Add `vol_clamp_floor_multiplier: 0.75` and `vol_clamp_ceiling_multiplier: 1.50` to `execution_gates.short_term_confirmation`.

**Pros:** Eliminates regime-shift distortion (~80% reduction per GPT). System-wide benefit, not just fat-tail tickers. Minimal complexity. Complements Fix 1 rather than replacing it.

**Cons:** Introduces two new policy parameters (clamp bounds). The 0.75/1.50 bounds are illustrative — need validation against historical data to confirm they don't over-constrain the EWMA during legitimate prolonged regime shifts. Slightly more compute (~5ms).

**Note on bounds validation:** The clamp bounds should be checked against `mws_ticker_history.csv` to verify: (a) how often the clamp binds per ticker, (b) that the floor doesn't prevent the EWMA from rising fast enough during genuine shock onset. This validation is recommended before finalising the bounds in policy.

---

### Fix 2 — Rolling empirical percentile for fat-tail tickers (Medium)

**Mechanism:** For fat_tail_monitor tickers, replace the EWMA-derived gate threshold with a rolling 252-day empirical percentile of 2-day returns. No normality assumption, no vol estimate.

```python
# At rebalance time, for fat_tail_monitor tickers:
returns_2d = price_history[ticker].pct_change(2).dropna().tail(252)
buy_gate  = np.percentile(returns_2d, 97.5)
sell_gate = np.percentile(returns_2d,  2.5)
```

Data availability: SIVR 392 obs ✓, IBIT 291 obs ✓ (≥254 required).

**Policy JSON change:** Add `gate_method: "empirical_p975"` to fat_tail_monitor entries.

**Runner change:** ~15 lines plus a branch.

**Pros:** No distributional assumption. Self-updating. No calibration. Partially immune to regime-shift distortion because it does not use a vol estimate.

**Cons:** Split gate method. Outlier stickiness (extreme events stay in window 252 days). Rolling window still lags abrupt structural breaks. Would have been unavailable at IBIT induction (only 291 obs today).

---

### Fix 3 — Student-t distribution fit (Too complex — presented for completeness)

Fit Student-t to each ticker's return history via MLE; use `t.ppf(0.975, df=ν)` as the threshold. Directly models fat tails but requires MLE fitting per ticker at each rebalance. Unstable for IBIT (291 obs < 500 recommended). `ν` estimates shift substantially between rebalances. Fix 2 achieves equivalent accuracy with far less complexity.

**Verdict:** Reject. Strictly dominated by Fix 2 on complexity, not on accuracy.

---

## 5. Comparison

| | Fix 1 | Fix 1b | Fix 2 | Fix 3 |
|---|---|---|---|---|
| New computation | None | std() clamp, ~5ms | np.percentile, ~10ms | MLE fit, ~200ms |
| Runner changes | ~2 lines | ~2 + 3 lines | ~15 lines + branch | ~50 lines + fitting |
| Fixes fat-tail shape | Yes | Yes | Yes | Yes |
| Fixes regime-shift distortion | No | Yes (~80%) | Partial | Partial |
| Adapts to fat-tail shape change | No (annual recal) | No (annual recal) | Yes (automatic) | Yes (unstable) |
| Threshold stability | High | High | Moderate (outlier dropout) | Low |
| New policy parameters | 2 (gate_sigma per ticker) | 4 (gate_sigma + clamp bounds) | 1 (gate_method flag) | None |
| Recommended | Baseline | **Yes, if regime-shift accepted** | If shape-drift is primary concern | No |

---

## 6. Reviewer Questions

1. **Fix selection:** Is Fix 1 (sigma override alone) sufficient, or should the vol clamp (Fix 1b) be adopted to also address regime-shift distortion? Or does Fix 2 (empirical percentile) better address both concerns at the cost of added complexity?

2. **IAUM:** IAUM has a 1.58pp EWMA/empirical gap (emp_p975 = 3.86%, EWMA 2σ = 5.44%). Below the 2pp threshold but same precious-metals fat-tail structure as SIVR. Include in the fix?

3. **Fix 1 sell sigma sensitivity:** SIVR `gate_sigma_sell = 1.0` fires on ~16% of trading days. Is this too sensitive? Alternative: round to 1.25σ for symmetry with buy-gate (fires ~10% of days).

4. **Vol clamp bounds (Fix 1b):** GPT proposed 0.75× floor / 1.50× ceiling as illustrative values. Are these bounds appropriate, or should they be validated against historical data before finalising? If so, what validation criteria should be used?

5. **Regime-shift scope:** The vol clamp in Fix 1b would apply system-wide (all tickers), not just SIVR/IBIT. Is it appropriate to expand the scope of a fat-tail fix to a system-wide structural change? Or should the clamp be deferred to a separate policy review?

6. **Recalibration cadence (Fix 1/1b):** How frequently should `gate_sigma` values be recalibrated? Options: (a) annually on policy review date, (b) dynamically at each rebalance (effectively becomes Fix 2), (c) manually when `ewma_emp_gap` in run log exceeds a threshold.

7. **Fix 3 rejection:** Do you agree Fix 3 should be rejected in favour of Fix 2 if higher complexity is warranted?

---

## 7. What Is Not Being Changed

- Global `buy_defer_sigma = 2.0` and `sell_defer_sigma = 2.5` — unchanged for all other tickers
- `max_defer_calendar_days = 10` — unchanged
- Stress regime sell-defer collapse to 3 days — unchanged
- Spike-trim logic — unchanged
- Turnover attribution rule — unchanged
- Momentum signal, blend weights, sleeve allocations — unchanged

---

*Brief prepared 2026-03-10. Source: `mws_policy.json` (v2.9.2), `mws_ticker_history.csv`. Regime-shift concern raised by ChatGPT review of prior draft.*
