# Momentum-Weighted Scaling (MWS) v2.6.16
## Macro Lens & Governance Addendum
**As-of:** 2026-01-24  
**Authoritative Source:** mws_policy.json  
**Role:** Advisory / explanatory only. Policy JSON is binding.

---

## 0. Scope & Precedence

This document explains how the Momentum-Weighted Scaling (MWS) system is intended to operate **given the current policy configuration**.  
If any conflict exists:

1. `mws_policy.json` is authoritative  
2. Execution code is authoritative over prose  
3. This document provides interpretation and macro context only

---

## 1. Portfolio Architecture

### Bucket A — Protected Capital
- Contents:
  - `TREASURY_NOTE` (fixed price: **$45,000**)
- Characteristics:
  - Excluded from momentum
  - Excluded from allocatable denominator
  - Serves as drawdown buffer and liquidity anchor
- Enforcement:
  - Hard minimum enforced by policy
  - Not eligible for trade funding

### Bucket B — Deployable Capital
- Contents:
  - All inducted assets
  - Activated assets (held but non-allocatable)
  - Cash
- Characteristics:
  - Subject to sleeve caps, floors, and turnover limits
  - Cash is included in the denominator but excluded from momentum

---

## 2. Asset Lifecycle & Eligibility (Policy-Exact)

| Lifecycle State | Allocatable | Momentum Eligible | Counts Toward Sleeves |
|-----------------|-------------|-------------------|-----------------------|
| reference       | No          | No                | No                    |
| activated       | No          | No                | No                    |
| inducted        | Yes         | Yes               | Yes                   |
| overlay         | No          | No                | No                    |

**Key rules**
- Only **inducted** tickers participate in optimization
- Activated tickers are live pilots:
  - No top-ups allowed
  - Trimmed only if explicit caps are breached
- Reference tickers are informational only

---

## 3. Momentum Engine

Momentum is computed **only on inducted tickers** using:

- 12-month total return (TR12)
- 6-month trend slope
- 3-month residual vs VTI

Weights:
- TR12: 45%
- Slope: 35%
- Residual: 20%

Normalization:
- Percentile-based within the inducted universe

Momentum determines **relative desirability**, not absolute allocation.

---

## 4. Risk & Correlation Governance

- Volatility: EWMA (126d) used internally
- Correlation anchors: VTI, QQQ
- Used to:
  - Penalize clustering
  - Select trim candidates when sleeves must shrink
- Momentum does not override risk constraints

---

## 5. Sleeve Structure

### Core Equity
- Broad market exposure (e.g. VTI, VXUS)
- Governed by global caps and floors

### AI / Technology
- Examples: SOXQ, BOTZ, CHAT
- High beta, liquidity-sensitive

### Biotech
- XBI
- Explicit sleeve floor enforced

### Real Assets
- Examples: SIVR, IAUM
- Governed by:
  - Sleeve cap (16%)
  - Per-ticker caps
- When trimming is mandatory, selection is risk-driven

### Defense / Energy
- Examples: ITA, XLE
- XLE has explicit band floor (2.5%)

### Stabilizers (Trend / CTA)
- DBMF, KMLM
- Rules:
  - Excluded from momentum
  - Excluded from denominator
  - Combined band: 6%–12%
  - Per-ticker band: 3%–6%
- Purpose:
  - Crisis convexity
  - Sellable liquidity
- Rebalanced manually or on band breach only

---

## 6. Crypto (Special Regime)

- IBIT
- Lifecycle: activated
- Excluded from momentum and denominator
- Governed by explicit caps and volatility stress rules
- Not part of core optimization

---

## 7. Cash Governance & Funding

- Cash:
  - Included in allocatable denominator
  - Excluded from momentum
- Does not:
  - Satisfy sleeve floors
  - Prevent hard cap enforcement

### Funding Invariant (Execution Layer)

```
cash_used + total_sells == total_buys
cash_used = min(cash_available, total_buys)
```

Implications:
- Residual cash is always used before generating sells
- If cash fully funds buys, no sells are required
- “Sells-first” refers to execution ordering only

---

## 8. Two-Stage Trim Logic

### Stage 1 — Target Convergence (Mandatory)
Triggered when:
- A sleeve exceeds target
- A cap is breached

Selection priority:
1. Risk contribution
2. Volatility
3. Correlation clustering

Momentum does not protect assets here.

### Stage 2 — Funding Relaxation (Optional)
Triggered when:
- Cash reduces required sells

Relaxation priority:
- Lowest momentum first
- Floor-bound assets are never relaxed

---

## 9. Turnover Governance

- Normal max turnover: 20%
- Stress max turnover: 22%
- Includes all buys and sells
- Enforced at execution time

---

## 10. Review Cadence (Advisory)

- Weekly: Momentum diagnostics
- Monthly: Lifecycle review
- Quarterly: Macro lens review

---

## 11. Design Philosophy (Non-Binding)

- Momentum selects what is attractive
- Risk controls how much is allowed
- Cash determines what is no longer required
- Policy is deterministic; macro is explanatory

---

**End of Document**

