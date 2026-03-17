# Momentum-Weighted Scaling (MWS) v2.9.6
## Governance Document
**As-of:** 2026-03-17
**Role:** Authoritative governance rationale. Read by `mws_runner.py` and injected into every LLM run as governance context.

---

## 0. Scope & Precedence

This document is **authoritative governance rationale** for the MWS system. It is injected into every LLM run prompt under the `GOVERNANCE RATIONALE` heading.
If any conflict exists:

1. `mws_policy.json` is authoritative for binding rules (caps, floors, signals, constraints)
2. Execution code is authoritative over prose
3. This document governs intent, architecture rationale, and invariants not expressed in JSON

---

## 1. Portfolio Architecture

### Bucket A — Protected Liquidity
- Contents: `TREASURY_NOTE` (US Treasury, CUSIP prefix 912)
- Minimum: **$45,000 market value** (live market price, not fallback)
- Characteristics:
  - Excluded from momentum
  - Excluded from allocatable denominator
  - Never used to fund trades
  - Hard minimum enforced at all times — survives even hard_limit drawdown

### Bucket B — Deployable Capital
- Contents: All inducted assets, overlays, and cash
- Cash is **included** in the allocatable denominator but excluded from momentum
- Basis for all L1/L2 sleeve cap and floor calculations (after overlay and Bucket A exclusions)

### Denominator Basis — Critical Distinction

Two different bases are used depending on the constraint type:

| Constraint Type | Basis | Formula |
|----------------|-------|---------|
| L1 sleeve caps | Allocatable denominator | TPV − overlays (DBMF+KMLM) − Bucket A (TREASURY_NOTE) |
| L2 sleeve caps/floors | Allocatable denominator | same |
| Per-ticker caps/floors | TPV | Full portfolio value including overlays and Bucket A |
| Overlay bands | TPV | Full portfolio value |

This distinction matters: a ticker can be within its per-ticker cap (vs TPV) while its sleeve is at its cap (vs denom), and vice versa.

---

## 2. Asset Lifecycle & Eligibility

| Lifecycle State | Allocatable | Momentum Eligible | Counts Toward Sleeves | Denominator |
|-----------------|-------------|-------------------|-----------------------|-------------|
| `reference`     | No          | No                | No                    | Excluded    |
| `inducted`      | Yes         | Yes               | Yes                   | Included    |
| `overlay`       | No          | No                | No (separate tree)    | Excluded    |

**Key rules:**
- Only **inducted** tickers participate in momentum optimization and sleeve allocation
- **Overlay** tickers (DBMF, KMLM) are governed separately via overlay bands, not sleeve caps
- `activated` stage exists in policy for compliance holds / manual review — currently no tickers in this state
- Reference tickers (QQQ, AGG, VIX, etc.) are informational benchmarks only

### Dormant Inducted Tickers
An inducted ticker with zero weight is dormant. Re-entry requires:
- Positive momentum blend for **15 consecutive days**
- VIX below **28**
- Initial weight at sleeve floor minimum

**IBIT exception (2026-03-05):** Entered via manual discretionary override of the lifecycle gate. Logged as compliance exception. Current position in `mws_holdings.csv`.

---

## 3. Portfolio Sleeve Structure (v2.8.5)

The portfolio is organized as a two-level hierarchy. All L1 and L2 caps are expressed as a percentage of the **allocatable denominator** unless otherwise noted.

### L1 Sleeves

| L1 Sleeve | Cap | Economic Driver | L2 Children |
|-----------|-----|-----------------|-------------|
| `growth` | 60% | Equity beta, AI capex, earnings growth | ai_tech, biotech, core_equity |
| `real_assets` | 25% | Commodity cycle, electrification, geopolitics | strategic_materials, defense_energy |
| `monetary_hedges` | 15% | Real rates, inflation, currency debasement, crisis hedge | precious_metals |
| `speculative` | 5% | High-volatility asymmetric | crypto |
| `stabilizers` | 6–12% TPV *(overlay, non-budgeted)* | Crisis alpha, trend following | managed_futures |

### L2 Sleeves

| L2 Sleeve | L1 Parent | Floor | Cap | Tickers |
|-----------|-----------|-------|-----|---------|
| `ai_tech` | growth | 22% (breadth-conditioned — see §3a) | 32% | SOXQ, CHAT, BOTZ, DTCR, GRID |
| `biotech` | growth | 4% | 12% | XBI |
| `core_equity` | growth | 18% | 38% | VTI, VXUS |
| `strategic_materials` | real_assets | 4% | 10% | URNM, REMX, COPX |
| `defense_energy` | real_assets | 6% | 14% | XLE, ITA |
| `precious_metals` | monetary_hedges | 8% | 15% | IAUM, SIVR |
| `crypto` | speculative | 0% | 5% | IBIT |
| `managed_futures` | stabilizers | 6% | 12% TPV | DBMF, KMLM |

### §3a — ai_tech Breadth-Conditioned Floor (v2.9.6)

The ai_tech sleeve floor is not a static value. It scales with sector breadth:

| Breadth State | Condition | Floor |
|---|---|---|
| Strong | ≥3 of 5 tickers with `blend_score > 0`, sustained ≥5 trading days | **22%** |
| Weak | <3 tickers positive, sustained ≥5 trading days | **12%** |
| Infeasible | 0 positive tickers OR ≥4 tickers in `floor_exit` state | **0%** (auto-released) |

**Rationale:** The 22% floor reflects a long-term structural conviction in AI/semiconductors as the portfolio's primary alpha source. The floor is intentional and is not a diversification artifact — it prevents premature abandonment of the thesis during normal corrections. However, a static 22% floor forced capital into weak ai_tech sub-components during mixed-signal regimes (when only 1–2 names were genuinely strong), diluting alpha by allocating to mediocre tickers to satisfy sleeve arithmetic.

The breadth-conditioned floor preserves full conviction when the sector is broadly healthy and reduces the forced dilution problem when it is not.

**5-day hysteresis:** The floor state does not change on the first day breadth crosses a threshold. The new state must persist for 5 consecutive trading days before the floor transitions. This prevents oscillation from marginal tickers hovering near zero momentum.

**Priority:** A breadth-triggered floor reduction (22% → 12%) is classified as Priority 3 (cap/floor compliance) in the execution hierarchy. It executes before signal-driven momentum trades, preventing the transition from being blocked by turnover budget already consumed by normal rebalancing.

**Monitoring:** Track (a) % of ai_tech sleeve weight allocated to sub-50th-percentile tickers (false breadth indicator) and (b) floor transition frequency (oscillation indicator).

### Design Rationale — v2.8.0 Restructure
Prior to v2.8.0, a single `defensive` L1 sleeve grouped gold, silver, uranium, copper, energy, and defense together under one cap. This created incoherent behavior: gold buys forced uranium sells; copper momentum was blocked by a "defensive" cap. The v2.8.0 restructure separates:
- **Monetary hedges** (gold/silver) — true crisis hedges, low equity correlation
- **Real assets** (commodity miners + energy/defense) — pro-cyclical growth trades, high beta

These two groups have opposite behavior in equity drawdowns. Mixing them in one sleeve was a structural defect.

---

## 4. Per-Ticker Constraints

All ticker caps and floors are expressed as a percentage of **TPV** (not allocatable denominator).

| Ticker | Min % TPV | Max % TPV | Notes |
|--------|-----------|-----------|-------|
| VTI | 10% | 25% | Core anchor; min_total enforced |
| VXUS | 4% | 12% | Floor ensures international diversification is structurally enforced |
| SOXQ | — | 10% | |
| CHAT | — | 8% | |
| DTCR | — | 6% | |
| BOTZ | — | 2% | Tight cap — low conviction position |
| GRID | — | 5% | Raised from 3% — allows momentum to express modest conviction bet |
| IAUM | — | 8% | |
| SIVR | 3% | 6% | Min and max enforced |
| XBI | — | 6% | |
| ITA | — | 10% | |
| XLE | — | 8% | |
| URNM | — | 4% | |
| REMX | — | 4% | |
| COPX | — | 3% | |
| IBIT | 0% | 5% | Dormant until signal gate cleared |
| DBMF | 3% | 6% | Overlay band |
| KMLM | 3% | 6% | Overlay band |

---

## 5. Momentum Engine

Momentum is computed **only on inducted tickers** using a blend of three signals:

| Signal | Weight | Description |
|--------|--------|-------------|
| 12-month total return (TR12) | 45% | Long-term trend |
| 6-month slope | 35% | Medium-term momentum |
| 3-month residual vs VTI | 20% | Short-term relative strength |

**Normalization:** Percentile-ranked within the inducted universe over a 63-day lookback window. Scores map linearly to target weight within each ticker's `[min_total, max_total]` band.

**Volatility:** EWMA (126-day lookback) used internally for risk adjustment.

**Floor behavior:** If momentum is positive, position is held at floor minimum. If momentum turns negative for 20 consecutive days, position is reduced to zero. Re-entry threshold (15 days positive) is intentionally shorter than exit threshold (20 days) — requires more conviction to exit than to re-enter.

---

## 6. Overlays (Managed Futures)

DBMF and KMLM are governed separately from the main sleeve tree:

- Excluded from momentum engine
- Excluded from allocatable denominator
- Target split: 50/50 DBMF/KMLM
- Combined band: 6%–12% of TPV
- Per-ticker band: 3%–6% of TPV
- Rebalanced manually or on band breach only
- Purpose: crisis alpha, trend following, diversifying return stream

The stabilizers sleeve provides negative correlation to equities during drawdowns — this is the primary reason they are excluded from the denominator. Including them would compress room for growth/real_assets in a way that defeats their purpose.

---

## 7. Cash Governance & Funding

- Cash is **included** in the allocatable denominator
- Cash does **not** satisfy sleeve floors or prevent hard cap enforcement
- Floors and caps determine target weights regardless of cash level

### Funding Invariant
```
cash_used + total_sells == total_buys
cash_used = min(cash_available, total_buys)
```

- Residual cash is always used before generating sells
- If cash fully funds all buys, total_sells must be zero
- "Sells-first" refers to execution ordering only, not funding priority

### Residual Allocation Rule (v2.9.5)
When sell proceeds or cash exceed compliance-required and floor-restoration buys, the residual deploys in this order:

1. **Underweight high-momentum inducted tickers** — sorted by momentum score descending. A ticker qualifies if current weight < percentile_in_band target AND momentum_score > 0, subject to per-ticker and sleeve caps.
2. **VTI** — receives any residual not absorbed by step 1, up to its max_total cap.
3. **Cash** — residual stays as cash if VTI is also fully capped.

**Rationale:** MWS is a barbell system. Sell proceeds should reinforce high-conviction momentum positions before flowing to the index anchor. Defaulting directly to VTI when strong momentum signals exist suppresses the system's alpha-generating mechanism. VTI is the last resort within Bucket B, not the default.

---

## 8. Constraint Precedence & Drawdown Rules

### Normal Operation — Priority Order
1. **Bucket A minimum** — never touched under any circumstance
2. **Per-ticker min_total** — holds during normal operation and soft_limit
3. **Overlay bands** — DBMF/KMLM maintained within 6–12% TPV
4. **L2 sleeve floors** — soft constraints, relaxed proportionally if infeasible
5. **Signal-driven optimization** — executes only after all above satisfied

### Drawdown Thresholds
| Level | Threshold | Action |
|-------|-----------|--------|
| Soft limit | 22% peak-to-trough | Freeze new buys; manual review required; min_total remains in force |
| Hard limit | 30% peak-to-trough | Reduce all positions toward sleeve floors; min_total may be overridden if floor reduction insufficient; override logged as compliance exception. Hard-limit compliance trades are **exempt from the 20% turnover cap** — they execute in full. |
| Recovery | <15% for 10 consecutive days, **OR** VTI positive momentum for 5 consecutive days | Resume normal rebalancing (whichever condition is met first) |

### Floor Conflict Resolution
If the optimization target set is **infeasible** — defined as: cannot simultaneously satisfy all hard constraints (Bucket A minimum, per-ticker caps, per-ticker min_total floors, overlay bands, turnover cap) given current TPV and allocatable denominator — then:
1. L2 sleeve floors are treated as soft and relaxed proportionally
2. Per-ticker min_total is satisfied first
3. Remaining capital allocated to sleeve floors proportionally
4. No engine may reduce a ticker below its min_total to satisfy a sleeve floor unless hard_limit is active

---

## 9. Rebalance Triggers

| Trigger | Condition | Notes |
|---------|-----------|-------|
| Calendar | First Monday of month | Standing monthly rebalance |
| Band breach | Any tracked band breached for 3 consecutive days | Applies to L2 sleeves, per-ticker caps, overlay bands |
| Signal drift | Sleeve drifts >5pp from target, or ticker drifts >1.5pp | Absolute percentage point delta |
| Stress override | soft_limit breached | Freezes calendar and signal_drift triggers; band_breach enforcement continues |

### Turnover Caps
- Per-rebalance event: **20%** (22% under soft_limit stress) — applies to **momentum-driven signal trades only**
- Annualized ceiling: **60%** (~2 major rotations per year)
- When turnover cap binds, partial rebalance executed in order of violation severity
- **Exception:** Hard-limit compliance trades (cap/floor enforcement, Bucket A protection, hard-limit position reduction) are exempt from all turnover caps and execute in full

---

## 10. Geopolitical Overlay (Disabled)

The `geopolitical_stress` overlay was disabled 2026-03-04 when XLE was reclassified back to the `defense_energy` L2 sleeve under `real_assets` L1. The overlay structure is retained in policy for potential future use. Advisory triggers (Brent spike ≥6% intraday, VIX ≥30, term structure inversion) remain defined but do not auto-execute.

---

## 11. Review Cadence (Advisory)

| Cadence | Activity |
|---------|----------|
| Daily | GAS runner: price fetch, perf log update, email digest |
| Weekly | Momentum diagnostics, band breach review |
| Monthly | Lifecycle review (inducted ticker review_days = 30) |
| Quarterly | Macro lens review, policy version audit |

---

## 12. Design Philosophy (Non-Binding)

- **Momentum** selects what is attractive
- **Risk controls** determine how much is allowed
- **Cash** determines what additional capacity exists
- **Policy is deterministic** — macro lens is explanatory
- **L1 sleeves** define economic regimes; L2 sleeves define tactical buckets within them
- **Precious metals hedge equities**; commodity miners amplify them — never mix in the same sleeve

---

**End of Document**

