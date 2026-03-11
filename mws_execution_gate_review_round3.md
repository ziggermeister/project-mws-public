# MWS Execution Gate — Round 3 Review Brief
**Policy:** MWS v2.9.1 (current state as of 2026-03-10)
**Prior version reviewed:** v2.9.0 (Round 2)
**Document purpose:** (1) Confirm what was adopted from Round 2 feedback. (2) Present our resolution of the Round 2 Item C divergence and request acknowledgment. (3) Request the full explanation of the spike-trim / cap-interaction edge case teased in Round 2. (4) Request final production sign-off on the execution gate.

---

## 1. What You Said — What Was Adopted (Round 2)

### ChatGPT Round 2 summary
| Your recommendation | Decision | Notes |
|---|---|---|
| Hidden interaction: confirm 3 mechanisms (mean-reversion tilt, trade supersession, signal drift amplification) ✅ | **Confirmed and logged** | All 3 mechanisms documented in policy. Accepted as structural trade-off. |
| Turnover: explicit rule needed, attribute to originating rebalance ✅ | **Adopted** | Full rule + `originating_rebalance_id` implementation note in policy |
| Item C (stress sell-defer): **disable entirely** during soft_limit | **Partially adopted** | Resolved as 3-day window — see Section 2 |
| Item D (vol formula): document derivation ✅ | **Adopted** | Both forms (`annual_vol / sqrt(126)` and `(annual_vol / sqrt(252)) × sqrt(2)`) now in policy |
| Spike-trim / cap-interaction edge case (teaser only — no full explanation yet) | **Flagged for Round 3** | Full explanation required — see Section 3 |

### Gemini Round 2 summary
| Your recommendation | Decision | Notes |
|---|---|---|
| Hidden interaction: confirm 2 mechanisms (mean-reversion tilt, trade supersession) ✅ | **Confirmed and logged** | ChatGPT's 3rd mechanism (signal drift amplification) also adopted |
| Turnover: originating-cycle attribution ✅ | **Adopted** | Your framing used verbatim |
| Item C (stress sell-defer): **accept full 10-day freeze** | **Partially adopted** | Resolved as 3-day window — see Section 2 |
| Item D (vol formula): both forms in compact notation ✅ | **Adopted** | |
| No additional open items raised | — | — |

---

## 2. Current Policy State: v2.9.1

### What changed from v2.9.0
| Parameter | v2.9.0 | v2.9.1 |
|---|---|---|
| Turnover accounting | No explicit rule | **Attributed to originating rebalance event; `originating_rebalance_id` tagging required** |
| Sell-defer during soft_limit | 10 calendar days (same as normal) | **3 calendar days** |
| Buy-defer during soft_limit | 10 calendar days | **10 calendar days** (unchanged) |
| Vol formula documentation | Simplified form only (`annual_vol / sqrt(126)`) | **Full derivation documented** |
| Hidden interaction | Unconfirmed | **Confirmed — 3 mechanisms logged, accepted as structural trade-off** |
| spike_trim_cap_interaction | Not present | **Flagged as open item, pending Round 3** |

### Gate logic as implemented (v2.9.1 — unchanged from v2.9.0)
```
At each rebalance, for each momentum-driven trade:

  daily_vol = annual_ewma_vol / sqrt(252)
  2d_vol    = daily_vol × sqrt(2)  =  annual_vol / sqrt(126)

  z = ret_2d(ticker) / 2d_vol

  if BUY  and z >= +2.0  → DEFER  (don't chase spike)
  if SELL and z <= -2.5  → DEFER  (don't sell into capitulation)
  if SELL and z >= +2.0  → SPIKE TRIM (execute immediately, flag)
  else                   → EXECUTE normally

  Normal regime:   deferred trade expires after 10 calendar days.
  Stress regime:   sell-deferred trade expires after 3 calendar days.
                   buy-defer unchanged at 10 days.
                   (stress = drawdown_rules.soft_limit_breached == true)

Hard overrides bypass gate unconditionally:
  - hard_limit_drawdown active
  - per_ticker_cap_breach
  - bucket_a_minimum_breach
```

---

## 3. Item C Resolution — Divergence Between Reviewers

### The divergence
In Round 2, Item C (interaction between sell-defer and soft_limit stress override) produced a direct conflict:
- **ChatGPT recommended:** Disable sell-defer entirely during soft_limit. Risk reduction must not be blocked. Capitulation protection can be temporarily suspended during a drawdown regime — panics eventually recover, but holding deteriorating positions for 10 days during a 20%+ drawdown compounds losses.
- **Gemini recommended:** Accept the full 10-day freeze as an intentional "do nothing during panic" rule. Panic-day execution typically occurs at worst prices. Selling into a capitulation event that triggers soft_limit is the highest-cost moment to execute.

### Our resolution: 3-day sell-defer during stress
We adopted a **middle path** — sell-defer is preserved but shortened from 10 days to **3 calendar days** when `soft_limit_breached == true`. Buy-defer remains at 10 days (no change needed since `stress_override.freeze_new_buys` already handles it).

**Rationale:**
- ChatGPT's concern (portfolio frozen during drawdown): addressed — sell-defer expires within 3 days, guaranteeing risk reduction can proceed within a narrow window even in panic.
- Gemini's concern (panic-day execution costs): addressed — 3-day window preserves protection against executing into the single worst-day capitulation move, while not freezing the portfolio indefinitely.
- The asymmetry (sell-defer 3 days / buy-defer 10 days) mirrors the directional asymmetry of the stress regime itself: reducing risk is urgent, adding risk is already frozen.

**Reviewer question for Item C:** Do both reviewers accept this middle path, or is there a remaining concern with the 3-day sell-defer window during soft_limit?

---

## 4. Round 3 Open Item — Spike-Trim / Cap Interaction

### Background
In your Round 2 review (ChatGPT), you wrote:

> *"There is one more edge case worth discussing before production sign-off: the interaction between spike-trim execution and the percentile_in_band cap enforcement can produce a temporary rank inversion trade under certain volatility regimes. It's subtle and not always harmful, but it's worth understanding."*

You did not provide the full mechanism in Round 2. We are requesting the complete explanation now.

### Our working hypothesis (may be wrong)

When spike-trim fires on a SELL trade (z ≥ +2.0, ticker has spiked), it executes immediately. However:

1. **Price-at-execution vs target-at-signal:** The percentile_in_band target was computed at the rebalance signal price. The spike-trim executes at the elevated spiked price. The proceeds from the sell are higher than the target assumed, meaning the portfolio now has more cash than the allocation engine expected.

2. **Cap re-trigger:** If the sell reduces the ticker below its min_total floor (unlikely but possible if the spike was large enough to move the ticker near its floor), the next rebalance may generate a compensating BUY for the same ticker — a buy immediately following a spike-trim sell, at a price that is now potentially still elevated. This could be the "rank inversion" effect: the momentum rank at the next rebalance may still be high (the spike hasn't reversed), so the ticker still has a high target weight, generating a buy after we just sold.

3. **Vol-regime dependency:** This would be more pronounced in high-vol tickers (SIVR, URNM, IBIT) where 2σ moves are larger in absolute terms, and where the gap between the spike-trim execution price and the signal-time target price is widest.

**Is this the mechanism you had in mind? Is there a different or additional effect?**

### Policy implications if confirmed
Depending on the mechanism, potential mitigations include:
- A cooldown rule: no buy of a ticker within N days of a spike-trim sell of the same ticker
- A target price lock: use signal-time price for turnover accounting regardless of spike-trim execution price
- No change needed: accept as low-frequency noise

We are not proposing any mitigation yet — we want the full explanation first.

---

## 5. Simulation Reference: Gate Applied to 2026-03-10 Rebalance (unchanged from Round 2)

| | No Gate | v2.9.1 Gate |
|---|---|---|
| Buys executed | $17,771 | $17,771 |
| Sells executed | $160,767 | $160,767 |
| Trades deferred | 0 | 0 |
| Spike-trim flags | — | 4 (CHAT +3.26σ, SOXQ +2.12σ, GRID +2.01σ, VXUS +2.00σ) |
| Turnover | 22.30% | 22.30% |

Note: No deferments fired on 2026-03-10 (no sell z ≤ -2.5, no buy z ≥ +2.0; URNM was +1.82σ, below buy-defer threshold).

The spike-trim flags on 2026-03-10 are all on the SELL side (z ≥ +2.0, direction = SELL). Under the gate: all four would execute immediately with a `spike_trim` annotation. None would be deferred. This is exactly as intended — selling after a 2-day spike is favourable timing.

---

## 6. Reviewer Checklist for Round 3

- [ ] **Item C resolution (stress sell-defer = 3 days):** Do you accept the middle path? Any remaining concern?
- [ ] **Spike-trim / cap interaction:** Provide the full mechanism. Is our hypothesis correct? Is there an additional effect?
- [ ] **Spike-trim / cap interaction — policy implication:** Does it require a mitigation rule, or is it acceptable as-is?
- [ ] **Overall v2.9.1:** Any remaining concerns not yet addressed? Safe to declare the execution gate production-ready?

---

## 7. Full Policy Block for Reference

The complete `execution_gates` section of `mws_policy.json` v2.9.1 is reproduced below for context.

```json
"execution_gates": {
  "_meta": {
    "added_version": "v2.9.0",
    "status": "active",
    "applies_to": "momentum_driven_trades_only",
    "does_not_apply_to": [
      "hard_limit_drawdown_reduction",
      "bucket_a_restoration",
      "cap_floor_compliance_enforcement",
      "turnover_cap_clipping"
    ],
    "spike_trim_annotation_applies_to": [
      "momentum_driven_trades",
      "cap_floor_compliance_sells"
    ]
  },
  "short_term_confirmation": {
    "enabled": true,
    "method": "z_score",
    "lookback_days": 2,
    "vol_source": "ewma_annual_vol",
    "ewma_span_days": 126,
    "vol_conversion_derivation": "daily_vol = annual_vol / sqrt(252); 2d_vol = daily_vol × sqrt(2) = annual_vol / sqrt(126). Both forms equivalent.",
    "sell_defer_sigma": 2.5,
    "buy_defer_sigma": 2.0,
    "sell_defer_if": "z_score <= -2.5",
    "buy_defer_if": "z_score >= +2.0",
    "spike_trim_if": "z_score >= +2.0 AND trade_direction == SELL",
    "spike_trim_effect": "execute_immediately_flag_as_spike_trim",
    "spike_trim_scope": "momentum_trades_and_cap_breach_sells",
    "max_defer_calendar_days": 10,
    "stress_regime_overrides": {
      "condition": "drawdown_rules.soft_limit_breached == true",
      "sell_defer_max_calendar_days": 3,
      "buy_defer_max_calendar_days": 10,
      "rationale": "Middle path. ChatGPT recommended disabling sell-defer; Gemini recommended accepting full freeze. 3-day window preserves panic protection while ensuring risk reduction within 3 days."
    },
    "turnover_accounting": {
      "rule": "deferred_trade_turnover_attributed_to_originating_rebalance_event",
      "implementation_note": "Tag each trade with originating_rebalance_id at signal generation time. Post turnover to originating_rebalance_id at settlement, not to the execution-date rebalance."
    },
    "hard_overrides": [
      "hard_limit_drawdown",
      "per_ticker_cap_breach",
      "bucket_a_minimum_breach"
    ]
  },
  "per_ticker_thresholds": {
    "_note": "Recomputed live at each rebalance. Values below are 2026-03-10 reference snapshot.",
    "SIVR":  { "vol_ann": 0.7955, "buy_gate": "+14.17%", "sell_gate": "-17.71%", "emp_p975": "+8.58%",  "flag": "fat_tail_monitor" },
    "URNM":  { "vol_ann": 0.5377, "buy_gate": "+9.58%",  "sell_gate": "-11.97%", "emp_p975": "+8.81%" },
    "IBIT":  { "vol_ann": 0.5186, "buy_gate": "+9.24%",  "sell_gate": "-11.55%", "emp_p975": "+6.88%",  "flag": "fat_tail_monitor" },
    "REMX":  { "vol_ann": 0.4684, "buy_gate": "+8.35%",  "sell_gate": "-10.43%", "emp_p975": "+7.84%" },
    "COPX":  { "vol_ann": 0.4569, "buy_gate": "+8.14%",  "sell_gate": "-10.18%", "emp_p975": "+6.93%" },
    "SOXQ":  { "vol_ann": 0.3232, "buy_gate": "+5.76%",  "sell_gate": "-7.20%",  "emp_p975": "+5.69%" },
    "IAUM":  { "vol_ann": 0.3056, "buy_gate": "+5.44%",  "sell_gate": "-6.81%",  "emp_p975": "+3.86%" },
    "CHAT":  { "vol_ann": 0.2948, "buy_gate": "+5.25%",  "sell_gate": "-6.57%",  "emp_p975": "+5.46%" },
    "XBI":   { "vol_ann": 0.2311, "buy_gate": "+4.12%",  "sell_gate": "-5.15%",  "emp_p975": "+4.37%" },
    "BOTZ":  { "vol_ann": 0.2147, "buy_gate": "+3.83%",  "sell_gate": "-4.78%",  "emp_p975": "+4.13%" },
    "DTCR":  { "vol_ann": 0.2105, "buy_gate": "+3.75%",  "sell_gate": "-4.69%",  "emp_p975": "+3.62%" },
    "ITA":   { "vol_ann": 0.1948, "buy_gate": "+3.47%",  "sell_gate": "-4.34%",  "emp_p975": "+3.76%" },
    "XLE":   { "vol_ann": 0.1850, "buy_gate": "+3.30%",  "sell_gate": "-4.12%",  "emp_p975": "+3.31%" },
    "GRID":  { "vol_ann": 0.1777, "buy_gate": "+3.17%",  "sell_gate": "-3.96%",  "emp_p975": "+3.16%" },
    "VXUS":  { "vol_ann": 0.1430, "buy_gate": "+2.55%",  "sell_gate": "-3.18%",  "emp_p975": "+2.24%" },
    "VTI":   { "vol_ann": 0.1182, "buy_gate": "+2.11%",  "sell_gate": "-2.63%",  "emp_p975": "+2.68%" }
  }
}
```

---

*Round 3 brief generated 2026-03-10. Policy file: `mws_policy.json` v2.9.1. No policy changes are pending — this brief is soliciting feedback before any further updates.*
