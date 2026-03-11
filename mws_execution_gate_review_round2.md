# MWS Execution Gate — Round 2 Review Brief
**Policy:** MWS v2.9.0 (live as of 2026-03-10)
**Prior version reviewed:** v2.8.5 proposal
**Document purpose:** Update reviewers on decisions taken from Round 1 feedback. Request Round 2 feedback on three open items.

---

## 1. What You Said — What Was Adopted

### ChatGPT Round 1 summary
| Your recommendation | Decision | Notes |
|---|---|---|
| Gate concept ✅ | **Adopted** | |
| Vol-scaling ✅ | **Adopted** | |
| sigma_threshold = 2.0 ✅ | **Adopted as buy_defer** | See asymmetric sigma below |
| Spike-trim ✅ | **Adopted** | |
| max_defer → calendar days ✅ | **Adopted: 10 calendar days** | |
| Fat-tail tickers → keep EWMA ✅ | **Adopted** | Monitor flags retained |
| Asymmetric sigma → defer, keep 2.0/2.0 | **Partially adopted** | Gemini's 2.5/2.0 adopted instead — see below |
| Gate scope → momentum-only | **Partially adopted** | Spike-trim annotation extended to cap-breach sells as informational only; compliance determinism preserved |
| Turnover acknowledged as separate | **Logged** | Out of scope for gate |
| 12m lookback unchanged ✅ | **Adopted** | |
| "Hidden interaction" teaser | **Logged as open item** | See Section 3 — requesting full explanation |

### Gemini Round 1 summary
| Your recommendation | Decision | Notes |
|---|---|---|
| Gate concept ✅ | **Adopted** | |
| Vol-scaling ✅ | **Adopted** | |
| sigma_threshold = 2.0 ✅ | **Adopted as buy_defer** | |
| Spike-trim ✅ | **Adopted** | |
| max_defer → 10 calendar days ✅ | **Adopted** | |
| Fat-tail tickers → keep EWMA ✅ | **Adopted** | |
| Asymmetric sigma: sell=2.5 / buy=2.0 ✅ | **Adopted** | ChatGPT deferred; Gemini's position prevailed |
| Extend spike-trim to cap-breach sells ✅ | **Adopted as annotation** | Execution remains unconditional; flag is informational |
| Turnover acknowledged ✅ | **Logged** | |
| 12m lookback unchanged ✅ | **Confirmed** | |

---

## 2. Current Policy State: v2.9.0

### What changed from the proposal
| Parameter | Proposed | Final v2.9.0 |
|---|---|---|
| sell_defer_sigma | 2.0 (symmetric) | **2.5** |
| buy_defer_sigma | 2.0 (symmetric) | **2.0** |
| max_defer_days | 3 rebalance cycles | **10 calendar days** |
| spike_trim_scope | momentum trades only | **momentum + cap-breach sells (annotation)** |
| Fat-tail handling | EWMA + monitor | **EWMA + monitor** (unchanged) |

### Gate logic as implemented

```
At each rebalance, for each momentum-driven trade:

  compute z = ret_2d(ticker) / (ewma_annual_vol / sqrt(126))

  if BUY and z >= +2.0  → DEFER  (don't chase spike)
  if SELL and z <= -2.5 → DEFER  (don't sell into capitulation)
  if SELL and z >= +2.0 → SPIKE TRIM (execute, flag as spike_trim)
  else                  → EXECUTE normally

  Deferred trade expires unconditionally after 10 calendar days.

Hard overrides bypass gate:
  - hard_limit_drawdown active
  - per_ticker_cap_breach
  - bucket_a_minimum_breach

Spike-trim annotation also applied to cap-breach sells (informational only).
```

### Per-ticker thresholds (v2.9.0 reference snapshot, 2026-03-10)

Thresholds are recomputed live at each rebalance from EWMA vol. The table below is the current snapshot for reference.

| Ticker | Ann Vol | Buy defer ≥ | Sell defer ≤ | Emp p97.5 | Flag |
|--------|---------|------------|-------------|-----------|------|
| SIVR | 79.5% | +14.17% | -17.71% | +8.58% | ⚠ fat_tail_monitor |
| URNM | 53.8% | +9.58% | -11.97% | +8.81% | |
| IBIT | 51.9% | +9.24% | -11.55% | +6.88% | ⚠ fat_tail_monitor |
| REMX | 46.8% | +8.35% | -10.43% | +7.84% | |
| COPX | 45.7% | +8.14% | -10.18% | +6.93% | |
| SOXQ | 32.3% | +5.76% | -7.20% | +5.69% | |
| IAUM | 30.6% | +5.44% | -6.81% | +3.86% | |
| CHAT | 29.5% | +5.25% | -6.57% | +5.46% | |
| XBI | 23.1% | +4.12% | -5.15% | +4.37% | |
| BOTZ | 21.5% | +3.83% | -4.78% | +4.13% | |
| DTCR | 21.1% | +3.75% | -4.69% | +3.62% | |
| ITA | 19.5% | +3.47% | -4.34% | +3.76% | |
| XLE | 18.5% | +3.30% | -4.12% | +3.31% | |
| GRID | 17.8% | +3.17% | -3.96% | +3.16% | |
| VXUS | 14.3% | +2.55% | -3.18% | +2.24% | |
| VTI | 11.8% | +2.11% | -2.63% | +2.68% | |

---

## 3. Round 2 Open Items — Requesting Feedback

### Item A: ChatGPT's "hidden interaction" — full explanation requested

In your Round 1 review you wrote:

> *"If you'd like, I can also tell you something important that no one has mentioned yet: There is one hidden interaction between the execution gate and the percentile_in_band allocation that could subtly bias the portfolio over time. It's not a bug, but it's a phenomenon worth understanding before deployment."*

Please provide the full explanation now. We have a working hypothesis but want your complete analysis before it is logged as a confirmed finding or dismissed.

**Our working hypothesis (may be wrong):**

When the gate defers a sell (ticker has dropped sharply, z ≤ -2.5), the position stays overweight. At the next rebalance the `percentile_in_band` engine recomputes targets from scratch — it has no memory of the deferred trade. Two effects may follow:

1. **Mean-reversion tilt:** The gate systematically holds more of tickers in sharp decline longer than the momentum signal intends. Over many cycles this could introduce a subtle buy-the-dip / hold-the-losers bias — the opposite of pure momentum.

2. **Trade supersession:** The deferred sell may never execute. If, at the next rebalance, the ticker's price has recovered and its new target weight is similar to its actual weight, the engine generates a smaller sell or none at all. The original deferred sell is superseded by fresh computation, not by a decision to cancel it.

3. **Asymmetric drift:** Because the sell-defer threshold (2.5σ) is higher than the buy-defer threshold (2.0σ), the gate defers sells less often than buys. But when sell-deferral does fire, the overweight position is likely larger (ticker dropped hard), compounding the holding effect.

Is this the interaction you had in mind, or is there a different or additional mechanism?

---

### Item B: Turnover double-counting risk

When the gate defers a trade across a rebalance boundary, a question arises for the turnover cap calculation (`max_turnover = 20% per rebalance event`):

**Scenario:** Trade is deferred at Rebalance T. At Rebalance T+1 the same trade is regenerated (or a similar one) and executes.

- Does the trade count against turnover at T+1 only? (It should — it didn't execute at T.)
- If the deferred trade auto-executes at day 10 (outside a scheduled rebalance), does it consume turnover budget from the next event or get accounted separately?
- Could this cause a rebalance at T+1 to exceed the turnover cap because it is effectively running two rebalances worth of trades?

**Reviewer question:** Does the current policy need explicit language governing how deferred-trade execution interacts with the `max_turnover` cap and the `max_turnover_annualized` ceiling? If yes, what should it say?

---

### Item C: Interaction with `stress_override` (soft drawdown)

The current policy has:

```json
"stress_override": {
  "condition": "drawdown_rules.soft_limit_breached == true",
  "action": "freeze_new_buys"
}
```

When `soft_limit` (20% peak-to-trough) is active, new buys are frozen. The execution gate adds a second mechanism that can defer buys. **During stress, both can be active simultaneously.**

Potential conflict:
- In a stress regime, most tickers are likely to be down. Some may be down sharply (z near -2.5σ or worse on the sell side).
- The gate may defer sells of declining tickers (capitulation protection).
- But `soft_limit` already freezes new buys, so only sells are in play.
- If the gate is also deferring those sells, the portfolio may be frozen — neither buying nor selling — for up to 10 calendar days during a drawdown event.

**Reviewer question:** Should the gate's sell-defer logic be suspended or tightened (e.g., threshold raised to 3.0σ) when `soft_limit` is active? Or is a 10-day freeze acceptable as an intentional "do nothing during panic" rule?

---

### Item D: vol_conversion denominator — sqrt(126) vs sqrt(252)

The current implementation uses:

```
2d_vol = annual_vol / sqrt(126)
```

This converts annual vol to a **2-trading-day** standard deviation, using the fact that 252 trading days / 2 = 126.

An alternative view: some practitioners use `sqrt(252)` as the divisor and then multiply by `sqrt(lookback_days)`:

```
daily_vol = annual_vol / sqrt(252)
2d_vol    = daily_vol × sqrt(2)
         = annual_vol × sqrt(2) / sqrt(252)
         = annual_vol / sqrt(126)    ← same result
```

These are equivalent. However, the policy currently states the formula as `annual_vol / sqrt(126)` without explaining the derivation. **Reviewer question:** Is the documentation of this formula sufficient, or should the policy note explicitly derive it as `(annual_vol / sqrt(252)) × sqrt(2)` for clarity and auditability?

---

## 4. Simulation Reference: Gate Applied to 2026-03-10 Rebalance

Provided for context. Full rebalance detail in original proposal.

| | No Gate | v2.9.0 Gate |
|---|---|---|
| Buys executed | $17,771 | $17,771 |
| Sells executed | $160,767 | $160,767 |
| Trades deferred | 0 | 0 |
| Spike-trim flags | 0 (fixed) / 1 (2σ fixed) | 4 (CHAT +3.26σ, SOXQ +2.12σ, GRID +2.01σ, VXUS +2.00σ) |
| Turnover | 22.30% | 22.30% |

Note: No deferments fired on 2026-03-10 because no sell had z ≤ -2.5 and no buy had z ≥ +2.0 (URNM was +1.82σ, below the buy-defer threshold of +2.0σ).

---

## 5. Reviewer Checklist for Round 2

- [ ] **Item A — Hidden interaction:** Confirm or correct our working hypothesis. Is there an additional mechanism beyond mean-reversion tilt and trade supersession?
- [ ] **Item B — Turnover double-counting:** Does the policy need explicit language? If yes, provide draft language.
- [ ] **Item C — Stress override interaction:** Should sell-defer be suspended or tightened during `soft_limit` active?
- [ ] **Item D — vol_conversion documentation:** Is current notation sufficient or should derivation be spelled out?
- [ ] **Overall v2.9.0:** Any remaining concerns with the gate as implemented? Safe to move to production?

---

*Round 2 brief generated 2026-03-10. Policy file: `mws_policy.json` v2.9.0. Source data: `mws_ticker_history.csv`, `mws_holdings.csv`.*
