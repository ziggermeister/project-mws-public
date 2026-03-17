# MWS Open Review Brief — March 2026
**Version:** v2.9.5
**Date:** 2026-03-17
**Purpose:** External LLM review — open-ended assessment of gaps, improvements, and profitability

---

## What We Are Asking

We are not asking you to evaluate whether the existing rules are internally consistent (we believe they are). We are asking you to step back and evaluate whether **this is the right system for what it is trying to accomplish**, and where the biggest gaps are in policy, implementation, or data.

Be direct. If you think a core design choice is wrong, say so and explain what you would do instead. If you think something is missing entirely, name it. We are not looking for reassurance — we are looking for the most valuable 3–5 improvements that would make this system more profitable and more robust.

---

## What This System Is

**MWS (Momentum-Weighted Scaling)** is a systematic, rules-based portfolio management system for a single IRA account (~$780K as of March 2026). It is designed and operated by an individual investor — not an institution.

### Core Goal
Outperform VTI (US total market index) by ≥150 basis points per year, net of trading costs, over a 10-year horizon. Secondary goal: limit peak-to-trough drawdowns.

### Hard Constraints That Cannot Change
- **Account type:** SEPP IRA (Substantially Equal Periodic Payments). Distributions must follow IRS 72(t) rules. $45K is withdrawn on January 5 each year. Early termination of the SEPP schedule before age 59½ triggers a 10% penalty on all prior distributions.
- **Instruments:** Liquid US-listed ETFs only. No individual stocks, no options, no futures, no derivatives.
- **Liquidity:** Must be able to trade the full position in a single session. All tickers are liquid large-cap ETFs.
- **Operator:** Single investor. No dedicated infrastructure. Runs on a Python script + LLM on a laptop or GitHub Actions.
- **Benchmark:** VTI (Vanguard Total Stock Market). The system must justify its complexity vs. just holding VTI.

### What the System Does
Each month (or on a band breach / drift trigger), the system:
1. Fetches latest prices for 20 ETFs
2. Computes momentum scores for each inducted ticker using a blend of 3 signals
3. Maps momentum scores to target weights using a percentile-in-band approach
4. Enforces a two-level sleeve hierarchy (8 sleeves across 5 L1 categories)
5. Applies an execution gate — defers or accelerates trades based on statistical extremity of recent price moves
6. Produces trade recommendations (buys/sells), which the human reviews and executes manually

---

## Portfolio Structure

### The Barbell Design Philosophy
The system is explicitly a **barbell**: one end is broad market beta (VTI as anchor, VXUS as complement), the other end is concentrated momentum bets in higher-conviction sectors. The goal is that the momentum-driven sectors provide alpha above what VTI alone would deliver.

### Bucket Structure
- **Bucket A (Protected Liquidity):** US Treasury Note, minimum $45,000 market value. Never touched. Provides multi-year SEPP withdrawal coverage in worst case.
- **Bucket B (Deployable Capital):** Everything else — inducted ETFs + cash + overlays.

### Sleeve Hierarchy
All L1 and L2 caps/floors are as % of **allocatable denominator** (TPV minus overlays minus Bucket A):

| L1 Sleeve | Cap | L2 Sleeve | Floor | Cap | Tickers |
|-----------|-----|-----------|-------|-----|---------|
| growth | 60% | ai_tech | 22% | 32% | SOXQ, CHAT, BOTZ, DTCR, GRID |
| growth | 60% | biotech | 4% | 12% | XBI |
| growth | 60% | core_equity | 18% | 38% | VTI, VXUS |
| real_assets | 25% | strategic_materials | 4% | 10% | URNM, REMX, COPX |
| real_assets | 25% | defense_energy | 6% | 14% | XLE, ITA |
| monetary_hedges | 15% | precious_metals | 8% | 15% | IAUM, SIVR |
| speculative | 5% | crypto | 0% | 5% | IBIT |
| stabilizers | 6–12% TPV | managed_futures | — | — | DBMF, KMLM |

DBMF and KMLM (managed futures overlays) are excluded from the denominator and governed separately at 6–12% of TPV.

### Per-Ticker Constraints (as % of TPV)
| Ticker | Min | Max | Role |
|--------|-----|-----|------|
| VTI | 10% | 25% | Core anchor |
| VXUS | 4% | 12% | International complement |
| SOXQ | — | 10% | Semiconductors |
| CHAT | — | 8% | AI software |
| BOTZ | — | 2% | Robotics/automation (low conviction) |
| DTCR | — | 6% | Data center infrastructure |
| GRID | — | 5% | Power grid infrastructure |
| IAUM | — | 8% | Gold |
| SIVR | 3% | 6% | Silver |
| XBI | — | 6% | Biotech |
| ITA | — | 10% | Defense |
| XLE | — | 8% | Energy |
| URNM | — | 4% | Uranium |
| REMX | — | 4% | Rare earths |
| COPX | — | 3% | Copper miners |
| IBIT | 0% | 5% | Bitcoin ETF |
| DBMF | 3% | 6% | Managed futures (overlay) |
| KMLM | 3% | 6% | Managed futures (overlay) |

---

## Momentum Engine

### Signal Blend
Momentum score = weighted blend of three signals, computed on inducted tickers only:

| Signal | Weight | Description |
|--------|--------|-------------|
| 12-month total return | 45% | Long-term trend |
| 6-month slope | 35% | Medium-term momentum |
| 3-month residual vs VTI | 20% | Short-term relative strength |

### Mapping to Weights
Momentum scores are percentile-ranked within the inducted universe over a 63-day lookback. The rank maps linearly to a target weight within each ticker's `[min_total, max_total]` band:

```
target_weight = min_total + (percentile/100) × (max_total - min_total)
```

This is a uniform linear mapping. No convex scaling, no regime-conditional adjustments.

### Floor/Exit Behavior
- Positive momentum → hold at floor minimum at minimum
- 20 consecutive days of negative momentum → reduce to zero
- Re-entry: 15 consecutive positive days + VIX < 28

---

## Risk Controls

| Level | Threshold | Action |
|-------|-----------|--------|
| Soft limit | 22% peak-to-trough | Freeze new buys; manual review |
| Hard limit | 30% peak-to-trough | Reduce all to sleeve floors; hard-limit trades exempt from turnover cap |
| Recovery | <15% for 10d OR VTI positive momentum 5d | Resume normal rebalancing |

Turnover caps: 20% per rebalance event (22% in stress); 60% annualized. Only applies to momentum-driven signal trades — compliance sells are uncapped.

---

## Execution Gate

A timing filter on momentum-driven trades only. Does not change target weights.

- Per-ticker EWMA vol-scaled z-score (126-day span, 2-day lookback)
- **Buy defer:** z ≥ +2.0σ → defer up to 10 days (don't chase spikes)
- **Sell defer:** z ≤ −2.5σ → defer up to 10 days (don't sell into capitulation)
- **Spike-trim:** z ≥ +2.0σ AND direction = SELL → execute immediately
- Vol is clamped between 0.75× and 1.50× of the 1-year realized vol to prevent gate malfunction in compressed or shock regimes

---

## What We Believe Is Working
To give you calibration on our current thinking:
- The sleeve hierarchy correctly separates asset classes that have different behaviors in drawdowns (e.g., gold vs uranium)
- The execution gate reduces whipsaw from chasing and capitulation selling
- Managed futures overlay (DBMF/KMLM) provides genuine crisis-alpha diversification
- Bucket A as a SEPP liquidity reserve removes the worst-case forced-selling scenario

---

## What We Are Uncertain About

We are genuinely uncertain about the following. These are starting points, not the full scope of your review:

### 1. Signal Architecture
The system uses momentum only — three backward-looking price signals. It has no:
- Valuation inputs (P/E, EV/EBITDA, yield spread)
- Quality factors (earnings growth, balance sheet strength)
- Macro regime awareness (interest rate environment, credit spreads, yield curve)
- Cross-asset signals (bond momentum, commodity momentum, dollar trend)

Is momentum-only the right approach for this type of account? If not, what would you add and how would you integrate it without making the system unmanageably complex for a single operator?

### 2. Percentile-in-Band Mapping
Target weights are determined by a linear percentile rank. This means:
- A ticker at the 80th percentile always gets the same weight as another ticker at the 80th percentile, regardless of how strong or weak the absolute signal is
- Scores are relative to the current inducted universe, which means if all tickers have negative absolute momentum, the top-ranked ticker still gets a large weight
- There is no absolute momentum floor — the system does not distinguish "everything is falling" from "some things are rising"

Is percentile-in-band the right mapping? Should there be an absolute momentum threshold below which no new buys are made? Should the mapping be nonlinear?

### 3. Ticker Selection
The 20 tickers were selected based on conviction-driven macro thesis. There is no systematic process for adding or removing tickers. The system does not evaluate whether a different set of ETFs would have higher expected alpha, nor does it look at correlation structure across the inducted universe.

Is there a better approach to universe construction and ticker selection?

### 4. SEPP Withdrawal Management
$45K is withdrawn every January 5. This is a fixed annual cash drain of ~5.8% of TPV (at current levels). There is currently no formal policy for how the portfolio prepares for this withdrawal — it is managed informally by the user. The system does not:
- Pre-position for the withdrawal (build up cash ahead of January)
- Optimize which assets to sell for the withdrawal (e.g., prefer selling low-momentum or over-weight tickers)
- Factor the withdrawal into target weight calculations

Is the current approach acceptable, or is the lack of a formal withdrawal management policy a material risk?

### 5. Alpha Source Validation
The system was designed with a specific thesis: that momentum in these ETF categories will outperform VTI by ≥150bps net of costs over a 10-year horizon. But:
- The system has not been running long enough for live validation
- There is no documented backtest against a specific period
- The 150bps target was set without formal analysis of expected factor premia in this universe

Is the expected alpha realistic? What is the primary alpha source in this design, and is it durable?

### 6. Operational Risk
The system runs on a combination of:
- Google Apps Script (daily price fetch, email digest)
- Python (momentum analytics)
- LLM (execution engine)
- Human (final trade execution in a brokerage UI)

There is no automated execution, no automated reconciliation, no audit trail beyond CSV files and git commits. The human can miss a rebalance, execute the wrong trade, or misread a recommendation.

Are there operational improvements that would materially reduce the risk of human error without requiring institutional infrastructure?

### 7. Drawdown Protection
The system's drawdown controls (soft/hard limits, overlay) are reactive — they respond after a drawdown has already occurred. There is no:
- Prospective hedging (put options, tail risk funds)
- Dynamic beta reduction as volatility rises
- Cross-asset signal that could reduce equity exposure before a drawdown begins

Given the account size ($780K) and SEPP withdrawal obligation, is the current drawdown framework adequate?

### 8. Open — What Are We Missing?
Beyond the above, what are the most significant gaps you see in this system? Consider:
- Structural design choices that seem wrong in principle
- Data the system uses but probably shouldn't (or ignores but should use)
- Well-established quant finance techniques this system is missing
- Risks that are not currently modeled or monitored

---

## What We Are NOT Asking
To avoid scope creep:
- Do not propose adding non-ETF instruments (stocks, options, futures, leveraged products)
- Do not propose adding institutional-grade infrastructure (Bloomberg, prime broker, dedicated server)
- Do not propose turning this into a tax-loss harvesting system — SEPP IRA rules make this largely irrelevant
- Do not propose removing the SEPP structure — it is fixed by IRS rules until age 59½
- Do not propose passive index investing — we accept that a systematic active system adds operational complexity and want to know if it can pay for itself

---

## How to Structure Your Response

We want your honest assessment, not a balanced summary. Format your response as:

**1. Biggest Gaps (top 3–5 items, ranked by expected impact on profitability and robustness)**
For each: What is the gap? Why does it matter? What would you do instead or in addition?

**2. What Looks Right**
Briefly: what design choices do you think are sound and should not be changed?

**3. Open Questions We Have Not Thought Of**
What did we not ask about that you think is important?

**4. One Structural Change Worth the Complexity**
If you could recommend one non-trivial change — something that adds some complexity but you believe would meaningfully improve the system — what would it be and why?

---

*MWS v2.9.5 | TPV ~$780K | IRA SEPP account | 10-year horizon | single operator*
