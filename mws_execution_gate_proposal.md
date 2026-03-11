# MWS Execution Gate — Policy Change Proposal
**Policy:** MWS v2.8.5 → proposed v2.9.0
**Date:** 2026-03-10
**Status:** Draft — awaiting LLM review and approval
**Scope:** Add `execution_gates` block to `mws_policy.json`. No changes to momentum signal, blend weights, lookback periods, sleeve caps/floors, or ticker constraints.

---

## 1. Problem Statement

The current MWS policy (v2.8.5) has no execution timing filter. The momentum engine computes target weights and the rebalance executes trades immediately at the next available price. This creates two failure modes:

**Failure mode A — Selling into capitulation.**
A ticker's 12-month momentum signal turns negative and a sell is generated. But if the ticker has already dropped sharply in the past 2 trading days, the sell executes into a depressed price, potentially locking in losses at the worst point of a short-term panic move. The 12-month signal is still correct directionally, but the *execution timing* is poor.

**Failure mode B — Buying into a spike.**
A ticker's momentum score is positive and a buy is generated. But if the ticker just spiked 2-day sharply (e.g., a geopolitical event lifting energy prices, or a short squeeze), buying into that spike chases a move that is likely to partially revert. Conversely, if the *sell* signal fires while a ticker is spiking, that is an ideal trim opportunity and should be *accelerated*, not deferred.

**What this proposal adds:**
An execution gate that checks whether the current 2-day price move is statistically extreme *for that specific ticker*, before executing a momentum-driven trade. If extreme:
- Sells into sharp drops are deferred (capitulation protection)
- Buys into sharp spikes are deferred (no chasing)
- Sells into sharp spikes are *confirmed as accelerated* (trim into strength)

**What this proposal does NOT change:**
- The 12-month momentum lookback (kept uniform — see Section 7)
- The momentum blend weights (45% tr12m / 35% slope6m / 20% res3m)
- Sleeve caps, floors, ticker constraints, or drawdown rules
- Hard compliance enforcement (cap/floor breaches and drawdown hard-limit bypass the gate entirely)

---

## 2. Design: Why Per-Ticker Vol-Scaled Thresholds

### 2.1 The fixed-threshold problem

A naive implementation would use a fixed threshold (e.g., ±8% over 2 days). This fails because the portfolio spans assets with annualised volatilities ranging from ~12% (VTI) to ~80% (SIVR):

- For **VTI** (11.8% ann vol): a 2-day move of ±8% represents roughly **7.6σ** — a once-in-decades event. The gate would almost never fire.
- For **SIVR** (79.5% ann vol): a 2-day move of ±8% represents roughly **1.1σ** — a completely routine day. The gate would fire on noise constantly.

A single absolute threshold produces fundamentally wrong behaviour at both extremes. This is a units problem: the threshold must be expressed relative to each ticker's own return distribution.

### 2.2 The solution: EWMA 2σ per-ticker threshold

The gate threshold for each ticker is:

```
2d_vol(t)       = ewma_annual_vol(t) / sqrt(126)
gate_threshold(t) = sigma_threshold × 2d_vol(t)
```

where:
- `ewma_annual_vol(t)` is the EWMA volatility (126-day span) **already computed by the momentum engine** — zero additional data cost
- `sqrt(126)` converts annual vol to a 2-trading-day standard deviation
- `sigma_threshold = 2.0` — the gate fires at approximately the 97.5th percentile of each ticker's own return distribution

A 2σ threshold means:
- The gate fires on roughly 2.5% of trading days per ticker (one-tailed, either side)
- It fires on events that are statistically significant for *that ticker*, not events that merely look large in absolute terms
- It naturally scales: low-vol tickers (VTI, VXUS) get tight thresholds; high-vol tickers (SIVR, URNM, IBIT) get wide thresholds

### 2.3 Empirical validation

The EWMA-derived 2σ threshold was cross-checked against the empirical 97.5th percentile of actual 2-day returns from `mws_ticker_history.csv`. They align well for most tickers (gap < 1pp). Two exceptions flagged in Section 6.

---

## 3. Gate Logic

```
For each momentum-driven trade at rebalance time:

  1. Compute z = ret_2d(t) / 2d_vol(t)
     where ret_2d = (price_today - price_2_days_ago) / price_2_days_ago

  2. If trade direction is BUY and z >= +sigma_threshold:
       → DEFER: do not execute buy this rebalance cycle
       → Reason: buying into a spike; wait for reversion
       → Defer for max max_defer_days, then execute regardless

  3. If trade direction is SELL and z <= -sigma_threshold:
       → DEFER: do not execute sell this rebalance cycle
       → Reason: selling into capitulation; wait for stabilisation
       → Defer for max max_defer_days, then execute regardless

  4. If trade direction is SELL and z >= +sigma_threshold:
       → ACCELERATE: execute immediately and flag as spike_trim
       → Reason: selling into strength is ideal trim timing
       → This is NOT a deferral; it is a confirmation

  5. Otherwise: execute normally

HARD OVERRIDES — gate is bypassed entirely if:
  - drawdown_rules.hard_limit is active
  - per-ticker cap is breached (ticker_constraints.max_total exceeded)
  - bucket_a_minimum is breached
```

The `max_defer_days` safety valve prevents a falling-knife situation where a ticker keeps dropping and the sell is perpetually deferred. After `max_defer_days` the trade executes unconditionally.

---

## 4. Per-Ticker Gate Thresholds

Computed from `mws_ticker_history.csv` as of 2026-03-10. Sorted by annualised volatility descending.

| Ticker | Sleeve | Ann Vol | 2d Vol | Gate ± (2σ) | Emp p2.5 | Emp p97.5 | EWMA vs Emp Gap | N obs |
|--------|--------|---------|--------|-------------|----------|-----------|-----------------|-------|
| SIVR | precious_metals | 79.5% | 7.09% | **±14.17%** | -6.89% | +8.58% | +5.59pp ⚠ | 392 |
| URNM | strategic_materials | 53.8% | 4.79% | **±9.58%** | -8.31% | +8.81% | +0.77pp | 356 |
| IBIT | crypto | 51.9% | 4.62% | **±9.24%** | -7.11% | +6.88% | +2.36pp ⚠ | 291 |
| REMX | strategic_materials | 46.8% | 4.17% | **±8.35%** | -7.39% | +7.84% | +0.50pp | 356 |
| COPX | strategic_materials | 45.7% | 4.07% | **±8.14%** | -7.67% | +6.93% | +1.21pp | 257 |
| SOXQ | ai_tech | 32.3% | 2.88% | **±5.76%** | -5.71% | +5.69% | +0.07pp | 392 |
| IAUM | precious_metals | 30.6% | 2.72% | **±5.44%** | -3.31% | +3.86% | +1.58pp | 392 |
| CHAT | ai_tech | 29.5% | 2.63% | **±5.25%** | -4.98% | +5.46% | -0.21pp | 392 |
| XBI | biotech | 23.1% | 2.06% | **±4.12%** | -5.16% | +4.37% | -0.25pp | 356 |
| BOTZ | ai_tech | 21.5% | 1.91% | **±3.82%** | -3.66% | +4.13% | -0.30pp | 392 |
| DTCR | ai_tech | 21.1% | 1.88% | **±3.75%** | -4.34% | +3.62% | +0.13pp | 392 |
| ITA | defense_energy | 19.5% | 1.74% | **±3.47%** | -2.84% | +3.76% | -0.29pp | 392 |
| XLE | defense_energy | 18.5% | 1.65% | **±3.30%** | -3.75% | +3.31% | -0.01pp | 379 |
| GRID | ai_tech | 17.8% | 1.58% | **±3.17%** | -3.34% | +3.16% | +0.01pp | 356 |
| VXUS | core_equity | 14.3% | 1.27% | **±2.55%** | -2.19% | +2.24% | +0.31pp | 392 |
| VTI | core_equity | 11.8% | 1.05% | **±2.11%** | -2.41% | +2.68% | -0.58pp | 392 |

**⚠ SIVR gap (+5.59pp):** EWMA-implied threshold (±14.17%) is materially wider than the empirical p97.5 (+8.58%). Silver returns are positively skewed and fat-tailed — the normal distribution assumption in EWMA overstates the upper threshold. This means the gate may rarely fire for SIVR even during genuine spike conditions. See Section 6, Open Question A.

**⚠ IBIT gap (+2.36pp):** Similar fat-tail issue for crypto. EWMA implies ±9.24% but empirical p97.5 is +6.88%. Gate is 2.36pp more permissive than actual history implies. See Section 6, Open Question A.

---

## 5. Simulation: 2026-03-10 Rebalance

The gate was applied to the current portfolio rebalance computed using the `percentile_in_band` allocation engine.

**Portfolio context:**
- TPV: $800,458.74
- Allocatable denominator: $684,629.07
- Cash available: $2,724.43
- 2-day window: 2026-03-08 → 2026-03-10

### 5.1 All trades with gate outcomes

| Ticker | Direction | Trade $ | 2d Return | Z-score | Gate Outcome |
|--------|-----------|---------|-----------|---------|--------------|
| CHAT | SELL | -$28,425 | +8.56% | **+3.26σ** | ★ SPIKE TRIM — execute (selling into strength) |
| XLE | SELL | -$25,432 | -0.46% | -0.28σ | Execute |
| VXUS | SELL | -$19,066 | +2.55% | **+2.00σ** | ★ SPIKE TRIM — execute |
| DTCR | SELL | -$15,456 | +3.11% | +1.66σ | Execute |
| GRID | SELL | -$12,340 | +3.18% | **+2.01σ** | ★ SPIKE TRIM — execute |
| SOXQ | SELL | -$11,216 | +6.10% | **+2.12σ** | ★ SPIKE TRIM — execute |
| BOTZ | SELL | -$10,952 | +1.97% | +1.03σ | Execute |
| IAUM | SELL | -$6,323 | +1.42% | +0.52σ | Execute |
| COPX | SELL | -$5,451 | +5.20% | +1.28σ | Execute |
| SIVR | SELL | -$3,801 | +6.80% | +0.96σ | Execute |
| ITA | SELL | -$2,059 | -0.18% | -0.10σ | Execute |
| REMX | SELL | -$375 | +5.70% | +1.36σ | Execute |
| VTI | BUY | +$14,530 | +1.33% | +1.26σ | Execute |
| IBIT | BUY | +$764 | +4.56% | +0.99σ | Execute |
| URNM | BUY | +$2,477 | +8.74% | +1.82σ | Execute (gate threshold ±9.58%, not breached) |

### 5.2 Comparison: no gate vs fixed ±8% vs vol-adj 2σ gate

| Metric | No Gate | Fixed ±8% Gate | Vol-adj 2σ Gate |
|--------|---------|---------------|-----------------|
| Buys executed | $17,771 | $15,294 | $17,771 |
| Sells executed | $160,767 | $160,767 | $160,767 |
| Trades deferred | 0 | 1 (URNM buy) | 0 |
| Spike-trim flags | 1 (CHAT) | 1 (CHAT) | 4 (CHAT, VXUS, GRID, SOXQ) |
| Turnover | 22.30% | 21.99% | 22.30% |

**Key finding:** The fixed ±8% gate incorrectly deferred the URNM buy (URNM's +8.74% move is only +1.82σ — a normal move for a 54%-vol commodity miner). The vol-adj gate correctly allows it. The vol-adj gate also identifies 3 additional spike-trim confirmations (VXUS, GRID, SOXQ) that the fixed gate missed entirely because their absolute returns were below 8% despite being statistically extreme for those tickers.

---

## 6. Open Questions for Reviewer

### A. SIVR and IBIT: EWMA vs empirical threshold

SIVR and IBIT exhibit fat-tailed return distributions where the EWMA-derived 2σ threshold is materially wider than the empirical 97.5th percentile:

| Ticker | EWMA 2σ gate | Emp p97.5 | Gap |
|--------|-------------|-----------|-----|
| SIVR | ±14.17% | +8.58% | +5.59pp |
| IBIT | ±9.24% | +6.88% | +2.36pp |

**Option 1 (proposed):** Use EWMA-derived threshold for all tickers uniformly. Simpler, consistent, adaptive to recent vol regime. Accept that SIVR and IBIT gates are slightly more permissive.

**Option 2:** Use empirical rolling p97.5/p2.5 (trailing 252 trading days of 2-day returns) for all tickers. Directly captures fat tails, no normality assumption. Slightly more complex to compute; requires 254+ days of history per ticker.

**Option 3:** Hybrid — use EWMA-derived for tickers with EWMA/empirical gap < 2pp; use empirical for tickers with gap ≥ 2pp (currently: SIVR, IBIT, IAUM).

**Reviewer question:** Is the SIVR/IBIT permissiveness a material concern given their relatively small position sizes (SIVR max 6% TPV, IBIT max 5% TPV)? Or does it warrant the added complexity of empirical percentiles?

---

### B. Asymmetric sigma threshold

The current proposal uses `sigma_threshold = 2.0` symmetrically for both sell-defer and buy-defer. An alternative is an asymmetric threshold:

- `sell_defer_sigma = 2.5` (higher bar for deferring a sell — a falling knife must be a more extreme drop before we refuse to sell)
- `buy_defer_sigma = 2.0` (standard bar for deferring a buy — don't chase spikes)

**Rationale for asymmetry:** Holding a deteriorating position too long (false sell-deferral) is more costly than missing a buy entry (false buy-deferral). The max_defer_days safety valve partially addresses this, but an asymmetric threshold provides a tighter guard.

**Reviewer question:** Should `sell_defer_sigma` and `buy_defer_sigma` be set identically (simpler, symmetric) or asymmetrically (2.5 / 2.0)?

---

### C. max_defer_days value

The proposed value is `max_defer_days = 3`. This means:
- After 3 rebalance cycles of deferral, the trade executes unconditionally
- Prevents indefinite deferral on a sustained trend (falling knife / sustained spike)
- At current rebalance frequency (monthly calendar + band-breach), 3 cycles ≈ up to 3 months maximum deferral

**Reviewer question:** Is `max_defer_days = 3` rebalance cycles appropriate? Should this be expressed in calendar days instead (e.g., 10 calendar days) to be frequency-independent?

---

### D. Gate scope: momentum-driven trades only

The gate applies **only to momentum-driven trades** (priority 5 in `governance.execution.trigger_precedence`). It does NOT apply to:
- Hard-limit drawdown forced reduction (priority 1)
- Bucket A restoration (priority 2)
- Cap/floor compliance enforcement (priority 3)
- Turnover cap clipping (priority 4)

**Reviewer question:** Is this scope correct? Specifically — should the spike-trim acceleration logic apply to cap-breach enforcement sells (priority 3)? For example, if XLE breaches its cap AND spiked +5% in 2 days, should that be explicitly flagged as an accelerated trim even though cap enforcement is already mandatory?

---

### E. Turnover note

The 2026-03-10 rebalance produces 22.30% turnover, which exceeds the `max_turnover` cap of 20% (though within `max_turnover_stress` of 22%). The gate does not change the turnover materially in this case. This is a pre-existing issue independent of the gate proposal — the large CHAT trim ($28,425) is the primary driver. The reviewer should note this is a separate concern to be addressed by the allocation engine, not the gate.

---

## 7. What Was Explicitly Kept Unchanged and Why

### 7.1 12-month momentum lookback — kept uniform

The question was raised whether the 12-month lookback should also be vol-scaled per-ticker (shorter lookback for high-vol assets like IBIT, longer for low-vol assets like VTI).

**Decision: No change.** The 12m signal feeds into a percentile ranking across the inducted universe — not an absolute threshold. Percentile ranking implicitly normalises cross-ticker volatility differences: IBIT achieving +60% annual return and VTI achieving +15% annual return can rank equally once normalised across the universe. The units problem that required vol-scaling for the execution gate does not exist in the momentum signal.

The blend weights (45% tr12m / 35% slope6m / 20% res3m) already capture short, medium, and long-term momentum at the signal level. Per-ticker blend weight tuning (e.g., more weight on slope6m for IBIT, more on tr12m for commodity miners) is a separate research question requiring backtesting — flagged as Round 3 work in the existing policy notes.

### 7.2 Sleeve caps, floors, ticker constraints — kept unchanged

The gate operates at the execution layer only. All target weights are computed identically to the current engine. The gate may defer a trade but never changes the target weight itself — the next rebalance will recompute targets from scratch using the then-current momentum scores and market prices.

---

## 8. Proposed Policy JSON Addition

The following block is to be added to `mws_policy.json` at the root level (alongside `signals`, `overlays`, `drawdown_rules`, etc.):

```json
"execution_gates": {
  "_meta": {
    "added_version": "v2.9.0",
    "added_date": "2026-03-10",
    "status": "active",
    "applies_to": "momentum_driven_trades_only",
    "does_not_apply_to": [
      "hard_limit_drawdown_reduction",
      "bucket_a_restoration",
      "cap_floor_compliance_enforcement",
      "turnover_cap_clipping"
    ],
    "notes": "Execution timing filter. Does not change target weights. Defers or accelerates trade execution based on statistical extremity of recent price move relative to each ticker's own volatility."
  },
  "short_term_confirmation": {
    "enabled": true,
    "method": "z_score",
    "lookback_days": 2,
    "vol_source": "ewma_annual_vol",
    "ewma_span_days": 126,
    "vol_conversion": "annual_vol / sqrt(126)",
    "sigma_threshold": 2.0,
    "sell_defer_if": "z_score <= -2.0",
    "buy_defer_if": "z_score >= +2.0",
    "spike_trim_if": "trade_direction == SELL AND z_score >= +2.0",
    "spike_trim_effect": "execute_immediately_flag_as_spike_trim",
    "max_defer_days": 3,
    "hard_overrides": [
      "hard_limit_drawdown",
      "per_ticker_cap_breach",
      "bucket_a_minimum_breach"
    ],
    "recompute_vol_at": "each_rebalance_event",
    "notes": [
      "Gate fires at approximately the 97.5th percentile of each ticker's own 2-day return distribution.",
      "SIVR and IBIT: EWMA-derived threshold is materially wider than empirical p97.5 (see open_questions.fat_tail_tickers). Monitor.",
      "sigma_threshold is the single tunable parameter. Increase to 2.5 for less sensitivity; decrease to 1.5 for more.",
      "Spike-trim is a confirmation, not a deferral: sells that fire during an upside z-score breach execute immediately."
    ],
    "open_questions": {
      "fat_tail_tickers": {
        "tickers": ["SIVR", "IBIT"],
        "issue": "EWMA 2sigma threshold wider than empirical p97.5 by 5.59pp (SIVR) and 2.36pp (IBIT) due to fat-tailed return distributions",
        "options": ["keep_ewma_uniform", "use_empirical_rolling_p975", "hybrid_by_gap_threshold"],
        "status": "unresolved_pending_review"
      },
      "asymmetric_sigma": {
        "question": "Should sell_defer_sigma (2.5) differ from buy_defer_sigma (2.0)?",
        "status": "unresolved_pending_review"
      },
      "max_defer_units": {
        "question": "Should max_defer_days be in rebalance cycles (current) or calendar days?",
        "status": "unresolved_pending_review"
      }
    }
  },
  "per_ticker_thresholds": {
    "_computed_date": "2026-03-10",
    "_source": "mws_ticker_history.csv",
    "_method": "ewma_126d_annualised / sqrt(126) * 2.0",
    "SIVR":  { "vol_ann": 0.7955, "vol_2d": 0.0709, "gate_pct": 0.1417, "emp_p975": 0.0858, "ewma_emp_gap": 0.0559, "flag": "fat_tail_monitor" },
    "URNM":  { "vol_ann": 0.5377, "vol_2d": 0.0479, "gate_pct": 0.0958, "emp_p975": 0.0881, "ewma_emp_gap": 0.0077 },
    "IBIT":  { "vol_ann": 0.5186, "vol_2d": 0.0462, "gate_pct": 0.0924, "emp_p975": 0.0688, "ewma_emp_gap": 0.0236, "flag": "fat_tail_monitor" },
    "REMX":  { "vol_ann": 0.4684, "vol_2d": 0.0417, "gate_pct": 0.0835, "emp_p975": 0.0784, "ewma_emp_gap": 0.0050 },
    "COPX":  { "vol_ann": 0.4569, "vol_2d": 0.0407, "gate_pct": 0.0814, "emp_p975": 0.0693, "ewma_emp_gap": 0.0121 },
    "SOXQ":  { "vol_ann": 0.3232, "vol_2d": 0.0288, "gate_pct": 0.0576, "emp_p975": 0.0569, "ewma_emp_gap": 0.0007 },
    "IAUM":  { "vol_ann": 0.3056, "vol_2d": 0.0272, "gate_pct": 0.0544, "emp_p975": 0.0386, "ewma_emp_gap": 0.0158 },
    "CHAT":  { "vol_ann": 0.2948, "vol_2d": 0.0263, "gate_pct": 0.0525, "emp_p975": 0.0546, "ewma_emp_gap": -0.0021 },
    "XBI":   { "vol_ann": 0.2311, "vol_2d": 0.0206, "gate_pct": 0.0412, "emp_p975": 0.0437, "ewma_emp_gap": -0.0025 },
    "BOTZ":  { "vol_ann": 0.2147, "vol_2d": 0.0191, "gate_pct": 0.0383, "emp_p975": 0.0413, "ewma_emp_gap": -0.0030 },
    "DTCR":  { "vol_ann": 0.2105, "vol_2d": 0.0188, "gate_pct": 0.0375, "emp_p975": 0.0362, "ewma_emp_gap": 0.0013 },
    "ITA":   { "vol_ann": 0.1948, "vol_2d": 0.0174, "gate_pct": 0.0347, "emp_p975": 0.0376, "ewma_emp_gap": -0.0029 },
    "XLE":   { "vol_ann": 0.1850, "vol_2d": 0.0165, "gate_pct": 0.0330, "emp_p975": 0.0331, "ewma_emp_gap": -0.0001 },
    "GRID":  { "vol_ann": 0.1777, "vol_2d": 0.0158, "gate_pct": 0.0317, "emp_p975": 0.0316, "ewma_emp_gap": 0.0001 },
    "VXUS":  { "vol_ann": 0.1430, "vol_2d": 0.0127, "gate_pct": 0.0255, "emp_p975": 0.0224, "ewma_emp_gap": 0.0031 },
    "VTI":   { "vol_ann": 0.1182, "vol_2d": 0.0105, "gate_pct": 0.0211, "emp_p975": 0.0268, "ewma_emp_gap": -0.0058 }
  }
}
```

---

## 9. Policy Version and Meta Update

If approved, the following meta fields should be updated:

```json
"meta": {
  "policy_version": "v2.9.0",
  "last_updated": "2026-03-10",
  "notes": "v2.9.0: Added execution_gates.short_term_confirmation. Vol-scaled 2-sigma per-ticker execution gate on 2-day return z-score. Applies to momentum-driven trades only. Hard compliance triggers bypass gate. 12m momentum lookback and blend weights unchanged."
}
```

---

## 10. Reviewer Checklist

Please confirm or provide feedback on each of the following:

- [ ] **Gate concept approved** — execution timing filter using 2-day z-score is the right mechanism
- [ ] **Per-ticker vol-scaling approved** — EWMA-derived 2σ threshold preferred over fixed ±8%
- [ ] **sigma_threshold = 2.0** — or recommend alternative value
- [ ] **Spike-trim logic approved** — sells that fire during upside z-score breach should execute immediately (not defer)
- [ ] **max_defer_days = 3** — or recommend alternative value or unit
- [ ] **Fat-tail tickers (SIVR, IBIT)** — accept EWMA approach with monitoring flag, OR require empirical percentile
- [ ] **Asymmetric sigma** — symmetric 2.0/2.0 acceptable, OR recommend sell_defer=2.5 / buy_defer=2.0
- [ ] **Gate scope** — momentum-driven trades only (priorities 1-4 bypass), OR extend spike-trim to cap-breach sells
- [ ] **Turnover overage** — acknowledged as separate issue from gate; to be addressed in allocation engine separately
- [ ] **12m lookback unchanged** — confirmed correct; no per-ticker variation needed

---

*Proposal generated by MWS allocation engine session, 2026-03-10. Source data: `mws_policy.json` (v2.8.5), `mws_ticker_history.csv`, `mws_holdings.csv`.*
