# MWS Design Review — Pre-Implementation Brief
**Date:** 2026-03-17
**Purpose:** Design review before finalizing implementation — solicit honest assessment of approach

---

## Context

I am an individual investor with a ~$780K IRA. I have designed a systematic portfolio management system called MWS (Momentum-Weighted Scaling) and am about to implement it in code. Before I commit fully, I want an honest assessment of the design from someone who has not been involved in building it.

**I am asking for your opinion on the approach — not validation of what I've already decided.**

If you think the core design is wrong, say so. If there is a better-known approach to this problem, tell me. If I am solving the wrong problem, tell me. I would rather hear hard feedback now than discover a structural flaw after 18 months of live operation.

---

## The Problem

### Account Facts
- **Account type:** SEPP IRA (IRS 72(t) Substantially Equal Periodic Payments)
  - I must take exactly $45,000 per year from this account on January 5, until I reach age 59½
  - If I break the SEPP schedule early, I owe a 10% penalty on **all prior distributions** — this is an extremely hard constraint
  - I cannot add money to this account; I can only withdraw
- **Current value:** ~$780,000
- **Instruments allowed:** Liquid US-listed ETFs only. No stocks, no options, no futures, no leveraged products.
- **Operator:** Me, a single individual. No dedicated infrastructure. I can run Python on a laptop and use GitHub Actions for scheduled tasks. I am technically capable but not a quant.

### What I Am Trying to Do
Beat VTI (Vanguard Total Stock Market ETF) by at least 150 basis points per year, net of trading costs, over a 10-year horizon. Secondary goal: limit peak-to-trough drawdowns, especially given the mandatory annual withdrawal.

I recognize that beating VTI over 10 years is hard. I also recognize that holding VTI alone exposes me to the sequence-of-returns risk that is particularly painful for accounts with mandatory annual withdrawals. So I want some active management that can rotate away from broad market beta when momentum signals deteriorate, while maintaining the capacity for meaningful upside when momentum is strong.

---

## My Proposed Approach

### Core Thesis
**Momentum works at the ETF level.** There is substantial academic and practitioner evidence that cross-sectional momentum (relative return ranking) across diversified asset class ETFs provides positive expected returns over horizons of 6–12 months, with Sharpe ratios above passive. I plan to exploit this by:
1. Holding a diversified set of liquid ETFs covering different economic regimes
2. Computing momentum scores for each, ranking them, and allocating more capital to higher-ranked tickers
3. Enforcing structural guardrails (min/max weights, sleeve caps, drawdown controls) to prevent the momentum signal from taking extreme positions

This is sometimes called a "risk parity with momentum tilt" or "sleeve-based momentum" approach. I am not trying to pick stocks — I am trying to rotate capital toward asset classes with positive momentum while maintaining diversification.

### Universe: 18 Inducted ETFs Across 5 Economic Regimes

I have organized the universe into sleeves that reflect different economic drivers:

| Economic Regime | Tickers | Rationale |
|----------------|---------|-----------|
| AI / Tech growth | SOXQ, CHAT, BOTZ, DTCR, GRID | Semiconductors, AI software, robotics, data center, power grid |
| Broad market beta | VTI, VXUS | US and international index anchors |
| Biotech | XBI | Healthcare innovation, uncorrelated to AI cycle |
| Real assets / Commodities | URNM, REMX, COPX, XLE, ITA | Uranium, rare earths, copper, energy, defense |
| Inflation / Crisis hedges | IAUM, SIVR | Gold and silver — negatively correlated to equities in crises |
| Crypto | IBIT | High-vol asymmetric bet, hard capped at 5% |
| Managed futures (overlay) | DBMF, KMLM | Crisis alpha, trend following — not in the momentum engine |

**Key design decision:** I deliberately separated gold/silver (monetary hedges) from commodity miners (real assets). They have opposite behavior in equity drawdowns — gold goes up, miners go down. Putting them in the same sleeve with a shared cap created incoherent behavior in my earlier design.

### Momentum Signal
I plan to compute a blend of three price-based signals for each inducted ticker:

| Signal | Weight | Rationale |
|--------|--------|-----------|
| 12-month total return | 45% | Long-term trend; dominant in the academic literature |
| 6-month price slope (linear regression) | 35% | Medium-term momentum; less subject to reversal than 12-month |
| 3-month residual return vs VTI | 20% | Relative strength vs market; catches sector rotation |

Scores are percentile-ranked within the inducted universe over a 63-day lookback. This produces a relative rank (0–100) for each ticker.

The rank maps linearly to a target weight within each ticker's allowed `[min_weight, max_weight]` band:
```
target_weight = min_weight + (percentile/100) × (max_weight - min_weight)
```

I chose linear mapping as the conservative starting point. I considered convex (favor high-rank) and concave (more uniform) mappings but have not validated either.

### Structural Guardrails

**Two-level sleeve hierarchy:**
- L1 sleeves cap the total allocation to each economic regime
- L2 sleeves define tactical bands within each regime

| L1 | Cap | L2 | Floor | Cap |
|----|-----|----|-------|-----|
| growth | 60% | ai_tech | 22% | 32% |
| growth | 60% | biotech | 4% | 12% |
| growth | 60% | core_equity | 18% | 38% |
| real_assets | 25% | strategic_materials | 4% | 10% |
| real_assets | 25% | defense_energy | 6% | 14% |
| monetary_hedges | 15% | precious_metals | 8% | 15% |
| speculative | 5% | crypto | 0% | 5% |

All caps/floors are as % of the allocatable denominator (TPV minus managed futures overlay minus Treasury reserve).

**Per-ticker caps:** Each ticker also has an individual max weight as % of TPV, preventing extreme concentration even if a sleeve has room.

**Protected liquidity (Bucket A):** I hold one US Treasury Note with a market value of ~$45,000. This is never touched — it represents approximately one full year of SEPP withdrawals in a hard asset that will survive even a severe equity drawdown.

**Managed futures overlay:** DBMF and KMLM (managed futures ETFs) are held at 6–12% of TPV outside the momentum engine. They are not ranked or momentum-allocated — they are maintained at a fixed band as a permanent crisis diversifier. I exclude them from the allocatable denominator so they don't compete with equity sleeves for budget.

**Drawdown controls:**
- 22% peak-to-trough → freeze all new buys, require manual review
- 30% peak-to-trough → force reduce all positions to sleeve floors

**Execution timing gate:** Before executing a momentum-driven trade, I check whether the 2-day price move was statistically extreme (z-score vs each ticker's EWMA vol). If a buy signal fires during a spike (z ≥ +2.0σ), defer up to 10 days. If a sell signal fires during a crash (z ≤ −2.5σ), defer up to 10 days. This prevents chasing spikes and panic selling into capitulations.

### Rebalance Triggers
I plan to rebalance:
1. First Monday of each month (standing calendar)
2. When any tracked band is breached for 3 consecutive days
3. When a sleeve drifts >5pp or a ticker drifts >1.5pp from target weight
4. Any time, if drawdown thresholds are breached

### Implementation Architecture
- **Price data:** Fetched daily from Stooq (free, no rate limits) via a Python script
- **Momentum computation:** Python (`mws_analytics.py`) computes all scores, z-scores, breach flags
- **Decision engine:** An LLM (Claude) reads the analytics output, the policy rules, current holdings, and recent news — and produces the trade recommendation in plain text
- **Execution:** I manually execute the recommended trades in my brokerage account
- **Automation:** GitHub Actions runs the analytics + LLM recommendation weekly, emails me the output
- **Policy rules:** All constraints encoded in a single JSON file (`mws_policy.json`) that the LLM reads on every run

The LLM acts as the execution engine, not a chatbot. It reads structured data, applies deterministic rules, and produces a specific trade list with rationale. Human judgment is preserved only at the final execution step.

---

## What I Am Uncertain About

I want your honest assessment of the following — but these are not the only things I want feedback on. If you see something I haven't asked about, say so.

### 1. Is momentum the right primary signal for this use case?
The ETF momentum literature is well-established, but I am applying it in a constrained IRA context with mandatory withdrawals. Most of the academic evidence is on mutual funds or futures, not on a 18-ticker ETF universe with hard sleeve floors. Am I over-fitting to a factor that may not translate cleanly?

### 2. Is the sleeve structure the right organizing principle?
I chose sleeves as a way to enforce diversification across economic regimes and prevent the momentum signal from going 100% into tech in a bull market. But sleeves introduce floors that force holdings even when momentum is negative. Is this the right trade-off? Is there a better way to enforce diversification without floors?

### 3. Is the percentile-in-band mapping sensible?
The linear mapping means a ticker at the 80th percentile gets the same target weight regardless of whether absolute momentum is +50% or +2%. There is no absolute momentum threshold — if all tickers are negative, the top-ranked one still gets a large weight. Is this a meaningful flaw? Should there be an absolute momentum gate before any allocation?

### 4. Should an LLM be in the execution loop?
I use an LLM to apply policy rules and incorporate news into the trade recommendation. The LLM can read the policy JSON, compute target weights, check gate conditions, search the web for news, and produce a final trade list. I believe this is better than pure Python because it handles edge cases, novel situations, and news context more gracefully. But I am aware that LLMs can be inconsistent and may make errors on quantitative calculations. Is this architecture sound? What are the failure modes?

### 5. Is the managed futures overlay sized correctly?
I hold 6–12% of TPV in DBMF + KMLM as a permanent non-momentum overlay. This is ~$47K–$94K at current portfolio value. The rationale is crisis alpha and trend diversification. But managed futures have underperformed in non-trending markets (2023–2024 choppy regime). Am I paying too high a diversification cost? Should this be smaller, larger, or conditional on the market regime?

### 6. What am I missing about the SEPP withdrawal constraint?
The $45K annual withdrawal (~5.8% of current TPV) is the most unusual aspect of this account. I have not built a formal pre-positioning policy for the withdrawal — I manage it informally by ensuring enough cash or near-cash is available in November/December. Is this adequate? What should a proper withdrawal management policy look like for a system like this?

### 7. What else?
What do you see in this design that I have not asked about? What would you change before writing the first line of code?

---

## What I Am NOT Asking For
- Passive index investing — I accept the complexity, I want to know if the active approach is designed well
- Instruments I cannot use (individual stocks, options, futures, leveraged ETFs)
- Institutional infrastructure (Bloomberg, prime broker, dedicated server)
- Tax-loss harvesting — this is a pre-tax IRA, irrelevant

---

## How to Structure Your Response

**Be direct. Rank your concerns by expected impact on the 10-year outcome.**

1. **What would you change before writing the first line of code?** (Top 3, with reasoning)
2. **What looks right and should be kept?** (Brief — just call out what you endorse)
3. **What risk or gap did I not ask about?** (The thing I missed entirely)
4. **What is the single most important design decision in this whole system?** (The one choice that will determine whether this outperforms or underperforms VTI over 10 years)

---

*Operator: individual investor | Account: SEPP IRA ~$780K | Horizon: 10 years | Withdrawal: $45K/year fixed*
