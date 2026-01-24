# Titanium MWS v3.0.5 Master Governance File (2026-01-16)

**Objective:** Maintain a robust, momentum-driven portfolio with strictly enforced risk tiers, multi-asset trend stabilizers, and a formal "Ghost vs. Citizen" lifecycle for new assets.

## 0. Stabilizer Sleeve Design
* **Objective:** Provide *sellable, non-correlated liquidity* during equity stress events.
* **Rules:** Stabilizer sleeve is **excluded from momentum ranking** and **excluded from momentum sleeve denominators**.
* **Instruments:**
    * **DBMF** (CTA replication / smoother profile)
    * **KMLM** (rules-based trend / higher convexity)
* **Combined stabilizer band:** **6%–12%** (Allows for drift).
* **Per-ticker band:** **3%–6%** each (Max 6% prevents nuisance rebalancing).
* **Initial target:** **~5% DBMF / ~5% KMLM** (10% total).
* **Execution rules (mechanical):**
    * Outside stress: rebalance only if either holding breaches its **3%–6%** band.
    * During equity stress: Manually shift Policy to "Gear 2" (Target 7% KMLM / 3% DBMF).

---

## 1. Inflation / Rates / Duration
Watch CPI/PCE, real yields, curve shape, and Fed guidance. Rising real yields pressure long-duration growth; falling yields support it. Rate shocks can break cross-asset correlations.

---

## 2. Credit / Liquidity Conditions
Watch spreads and liquidity. Tight liquidity punishes high beta/thematic ETFs.

---

## 3. Equity Breadth / Leadership
Watch breadth and leadership concentration. Narrow breadth can mask fragility even when momentum looks strong.

---

## 4. Commodities / Energy / Supply Shocks
Watch oil shocks, disruptions, and OPEC dynamics. Second-order effects flow through inflation expectations and policy tightening risk.

---

## 5. USD / FX / EM Risk
Watch Dollar Index and EM stress. Strong USD often pressures EM and some commodity complexes.

---

## 6. AI / Semis Cycle
Watch AI capex and the semiconductor cycle. Momentum can persist; reversals can be abrupt.

---

## 7. Crypto Regime
Crypto correlations can change quickly; treat as its own regime. **IBIT remains a ranked discrete asset exposure** under policy floors/caps.

---

## 8. Geopolitics / Defense
Markets typically transmit geopolitics through energy, inflation expectations, and risk sentiment. Defense reacts more to sustained posture/procurement than single headlines.

---

## 9. SEPP Liquidity Governance (Exact Thresholds)
**Objective:** Maintain sufficient SEPP liability coverage using Bucket A (Treasuries-only), without relying on discretionary forced sales.

**Bucket A definition:** U.S. Treasuries only (CUSIP prefix **912***).

**Exact thresholds:**
* **ERROR (violation):** Bucket A Liquidity < **$45,000**
* **WARNING:** Bucket A Liquidity < **$90,000**

---

## 10. Overlay Assets: DBMF/KMLM Treatment
**Classification:** Overlays / Multi-Asset Strategy ETFs.
* **Momentum treatment:** Excluded from MWS momentum ranking.
* **Interaction:** DBMF/KMLM are **multi-asset strategy wrappers**, separate from discrete assets like IAUM/IBIT.

---

## 11. Macro Awareness Control (Required Pre-Run Step)
Before any MWS run is interpreted or acted upon, the operator must review macro context to interpret risk and qualify confidence. Macro context must **never** override momentum rankings or modify caps/floors mid-run.

---

## 12. Ticker Lifecycle & Admission Protocol (v3.0.5)

**Objective:** Enforce a strict "Up or Out" progression. Prevent portfolio bloat by treating new assets as "Ghosts" until they earn "Citizen" status.

### **Stage 1: Experimental (The Sandbox)**
* **Role:** Watchlist / Paper Trade.
* **Capital:** 0% – 1%.
* **Governance:** **Ghost.** Excluded from Sleeve Floors & Caps.
* **Kill Rule:** 90 Days. If momentum/thesis fails, delete.

### **Stage 2: Activated (Proof of Work)**
* **Role:** Live Pilot. Skin in the game.
* **Capital:** 2.0% – 2.5% (Strict).
* **Governance:** **Ghost.** Excluded from Sleeve Floors & Caps.
* **Drift:** Excess > 2.5% is trimmed at rebalance. No top-ups allowed.
* **Current Status:**
    * **URNM:** **ACTIVATED** (Stage 2).
    * **QTUM:** **ACTIVATED** (Stage 2).
    * **IBIT:** **ACTIVATED** (Permanent Pilot).

### **Stage 3: Probation (The Gauntlet)**
* **Role:** Full Allocation on Short Leash.
* **Capital:** Full Target (e.g., 4%).
* **Governance:** **Citizen.** Counts toward Sleeve Floors & Caps.
* **Review:** Monthly. Hard breach = Demotion.
* **Current Status:** **XBI, BOTZ, CHAT.**

### **Stage 4: Inductee (The Core)**
* **Role:** Structural Infrastructure.
* **Governance:** **Citizen.** Managed purely by MWS Math.
* **Current Status:** **VTI, VXUS, QQQM, SOXQ, ITA, XLE, IAUM, SIVR, GRID, DTCR.**
