# MWS System — Comprehensive Peer Review Brief
**Date:** 2026-03-17
**Portfolio value (broker close):** $781,755.50
**System version:** MWS v2.9.4
**Purpose:** Solicit independent analysis on system robustness, profitability, and design blind spots from peer LLMs (Gemini, ChatGPT, or other reviewers). All numbers in this document are live as of market close 2026-03-17.

---

## 1. What This System Is

**Momentum-Weighted Scaling (MWS)** is a fully systematic, rules-based portfolio management system for a US self-directed IRA with annual SEPP (Substantially Equal Periodic Payment) withdrawals of $45,000 on January 5 each year.

**Objective:** Outperform VTI (US total market ETF) by ≥150bps net of all trading costs (assumed 8bps per dollar traded) over a 10-year horizon, while maintaining the SEPP liquidity requirement.

**Account type:** Self-directed IRA — no capital gains tax, but all withdrawals are taxed as ordinary income. No wash-sale rules apply.

**Automation:** The system runs twice daily on weekdays via GitHub Actions:
- 14:30 UTC: LLM run (momentum computation → trade recommendations → email)
- 21:30 UTC: Price fetch (Stooq historical + Stooq RT same-day fallback)

All binding rules live in `mws_policy.json`. An LLM (Claude) executes the run protocol, computes targets, applies the gate, and outputs structured trade recommendations. The LLM cannot modify policy — it only applies it.

---

## 2. Portfolio Architecture

### Denominator Basis — Critical Distinction

The system uses two different denominators depending on the constraint type:

| Constraint | Basis | Formula |
|-----------|-------|---------|
| L1 and L2 sleeve caps/floors | **Allocatable denominator** | TPV − TREASURY_NOTE − DBMF − KMLM |
| Per-ticker caps/floors | **TPV** | Full portfolio value |
| Overlay bands | **TPV** | Full portfolio value |

**As of 2026-03-17:**
- TPV: $781,755.50
- TREASURY_NOTE (Bucket A): $45,194.06
- Overlays (DBMF + KMLM): $71,706.71
- **Allocatable denominator: $664,854.73**

### Bucket Structure

**Bucket A — Protected Liquidity**
- Holds: US Treasury Note (CUSIP 91282CME8, due 12/31/2026, 4.25%)
- Minimum: $45,000 market value at all times
- Never touched, never pledged, never sold — survives even the hard drawdown limit
- Not momentum-eligible, excluded from all sleeve calculations

**Bucket B — Deployable Capital**
- All other assets: inducted holdings + overlays + cash ($0.89)
- Sleeve targets computed on the allocatable denominator

---

## 3. Sleeve Hierarchy

All caps/floors are % of **allocatable denominator** unless marked (TPV).

### L1 Sleeves

| L1 | Cap | Economic Driver |
|----|-----|-----------------|
| growth | 60% | Equity beta, AI capex, earnings growth |
| real_assets | 25% | Commodity cycle, electrification, geopolitics, energy |
| monetary_hedges | 15% | Real rates, inflation, currency debasement, crisis hedge |
| speculative | 5% | High-volatility asymmetric |
| stabilizers | 6–12% TPV *(overlay, excluded from denominator)* | Crisis alpha, trend following |

### L2 Sleeves — Current Status (2026-03-17)

| L2 | L1 | Floor | Cap | Tickers | Current Value | Current % | Status |
|----|-----|-------|-----|---------|--------------|-----------|--------|
| ai_tech | growth | 22% | 32% | SOXQ, CHAT, BOTZ, DTCR, GRID | $203,566 | 30.6% | ✅ |
| biotech | growth | 4% | 12% | XBI | $39,124 | 5.9% | ✅ |
| core_equity | growth | 18% | 38% | VTI, VXUS | $160,366 | 24.1% | ✅ |
| strategic_materials | real_assets | 4% | 10% | URNM, REMX, COPX | $62,974 | 9.5% | ✅ |
| defense_energy | real_assets | 6% | 14% | XLE, ITA | $93,619 | **14.1%** | ⚠️ **CAP BREACH** |
| precious_metals | monetary_hedges | 8% | 15% | IAUM, SIVR | $95,432 | 14.4% | ✅ |
| crypto | speculative | 0% | 5% | IBIT | $9,764 | 1.5% | ✅ |
| managed_futures (overlay) | stabilizers | 6% | 12% TPV | DBMF, KMLM | $71,707 | 9.2% TPV | ✅ |

**Active breach:** defense_energy at 14.1% vs 14.0% cap. Requires minimum XLE sell to restore compliance.

### Design Rationale — v2.8.0 Restructure

Prior to v2.8.0, a single `defensive` L1 sleeve grouped gold, silver, uranium, copper, energy, and defense together under one cap. This caused gold buys to force uranium sells; copper momentum was blocked by a "defensive" cap.

The v2.8.0 restructure separates:
- **Monetary hedges** (gold/silver): true crisis hedges, negative equity correlation
- **Real assets** (commodity miners + defense/energy): pro-cyclical, high equity beta

These two groups have **opposite** behavior during equity drawdowns. The prior design was a structural defect.

---

## 4. Per-Ticker Constraints

All expressed as % of **TPV**. Momentum score maps linearly within the [min_total, max_total] band.

| Ticker | L2 Sleeve | Min % TPV | Max % TPV | Notes |
|--------|-----------|-----------|-----------|-------|
| VTI | core_equity | **10%** | 25% | Only ticker with a per-ticker floor |
| VXUS | core_equity | 0% | 15% | |
| SOXQ | ai_tech | 0% | 10% | |
| CHAT | ai_tech | 0% | 8% | |
| DTCR | ai_tech | 0% | 6% | |
| BOTZ | ai_tech | 0% | **2%** | Tight cap — low conviction |
| GRID | ai_tech | 0% | 3% | |
| IAUM | precious_metals | 0% | 8% | |
| SIVR | precious_metals | **3%** | 6% | Both floor and cap enforced |
| XBI | biotech | 0% | 6% | |
| ITA | defense_energy | 0% | 10% | |
| XLE | defense_energy | 0% | 8% | |
| URNM | strategic_materials | 0% | 4% | |
| REMX | strategic_materials | 0% | 4% | |
| COPX | strategic_materials | 0% | 3% | |
| IBIT | crypto | 0% | 5% | Entered via discretionary override 2026-03-05 |
| DBMF | managed_futures | 3% TPV | 6% TPV | Overlay |
| KMLM | managed_futures | 3% TPV | 6% TPV | Overlay |

**Why VTI has a hard floor:** VTI is the core anchor of the portfolio. Even at zero momentum (50th percentile by definition, as it IS the benchmark), VTI must hold 10% TPV. This prevents the model from going to zero in the broad market when every other asset is outperforming it.

**Why SIVR has a floor:** Silver is the secondary monetary hedge. A 3% floor prevents complete liquidation during momentum weakness that may be transient (silver can have sharp drawdowns while gold holds, then recover).

---

## 5. Momentum Engine

### Signal Blend

| Signal | Weight | Window | Description |
|--------|--------|--------|-------------|
| TR12m | 45% | 12-month | Total return including dividends |
| Slope6m | 35% | 6-month | Linear slope of price series (trend strength) |
| Res3m | 20% | 3-month | Residual return vs VTI (relative strength) |

### Percentile Ranking

The blend score for each inducted ticker is **percentile-ranked** within the full inducted universe over a 63-day lookback. This means rankings are relative, not absolute — a down-market still has a 100th-percentile ticker.

### Target Weight Mapping

```
target_%_TPV = min_total + (percentile_rank / 100) × (max_total - min_total)
```

Linear interpolation within each ticker's `[min_total, max_total]` band. At 0th percentile, ticker is at min_total. At 100th, at max_total.

### Floor Exit / Re-Entry

| Event | Condition | Action |
|-------|-----------|--------|
| Floor exit | 20 consecutive days of negative momentum blend | Reduce position to zero |
| Re-entry | 15 consecutive positive days + VIX < 28 | Rebuild from sleeve floor minimum |

**Asymmetry is intentional:** 20 days to exit (more conviction required), 15 days to re-enter (don't miss rallies). Both are counting rules — a single neutral day does not reset the counter.

### Current Momentum Rankings (2026-03-17)

| Rank | Ticker | Percentile | Raw Score | Alpha vs VTI | Sleeve |
|------|--------|-----------|-----------|--------------|--------|
| 1 | URNM | 100.0% | 2.81 | +9.3% | strategic_materials |
| 2 | SIVR | 94.4% | 2.29 | +10.7% | precious_metals |
| 3 | COPX | 88.9% | 2.03 | +8.3% | strategic_materials |
| 4 | GRID | 83.3% | 1.97 | +9.8% | ai_tech |
| 5 | IAUM | 77.8% | 1.13 | +17.1% | precious_metals |
| 6 | CHAT | 72.2% | 0.99 | +9.8% | ai_tech |
| 7 | SOXQ | 66.7% | 0.90 | +8.2% | ai_tech |
| 8 | ITA | 61.1% | 0.85 | +6.6% | defense_energy |
| 9 | XLE | 55.6% | 0.85 | +29.9% | defense_energy |
| 10 | VTI | 50.0% | 0.83 | 0.0% | core_equity |
| 11 | REMX | 44.4% | 0.56 | +19.1% | strategic_materials |
| 12 | IBIT | 38.9% | 0.47 | -15.3% | crypto |
| 13 | BOTZ | 33.3% | 0.41 | -1.9% | ai_tech |
| 14 | DTCR | 27.8% | 0.34 | +16.7% | ai_tech |
| 15 | VXUS | 22.2% | 0.24 | +4.4% | core_equity |
| 16 | XBI | 16.7% | 0.11 | +3.9% | biotech |
| 17 | DBMF | 11.1% | 0.07 | +12.1% | managed_futures (overlay) |
| 18 | KMLM | 5.6% | 0.02 | +9.9% | managed_futures (overlay) |

**Notable observations:**
- Precious metals (SIVR, IAUM) and uranium (URNM) dominate rankings — risk-off / real-assets momentum
- VXUS at 22nd percentile despite +4.4% alpha vs VTI over 12m (USD headwind and earnings revision effect)
- DTCR at 28th percentile despite +16.7% alpha — strong recent underperformance dragging 6m slope
- XLE at 56th percentile (near-median) but +29.9% alpha — very wide EWMA vol means slope score is muted

---

## 6. Execution Gate (v2.9.4)

A timing filter applied to **momentum-driven trades only**. It does not change target weights — only defers execution when moves are statistically extreme (chase prevention + panic prevention).

### Mechanism

For each pending trade, compute a z-score of the 2-day price move relative to its EWMA volatility:

```
z = 2-day_log_return / effective_vol_2d

where:
  ewma_vol = sqrt(EWMA variance of daily log returns, 126-day span) × sqrt(252)
  rv1y = rolling 252-day realized vol of daily log returns, annualized
  effective_vol = clamp(ewma_vol, 0.75 × rv1y, 1.50 × rv1y)   ← vol_clamp (v2.9.4)
  effective_vol_2d = effective_vol / sqrt(252) × sqrt(2)
```

### Gate Rules

| Condition | Action | Max deferral |
|-----------|--------|-------------|
| BUY z ≥ +2.0σ | Defer — don't chase a spike | 10 calendar days |
| SELL z ≤ −2.5σ | Defer — don't sell into capitulation | 10 calendar days |
| SELL z ≥ +2.0σ | Execute immediately — spike-trim (sell into strength) | n/a |
| During soft_limit | Sell-defer max collapses to 3 calendar days | — |

### Per-Ticker Sigma Overrides

Fat-tail calibration audit (2026-03-11) revealed SIVR and IBIT have wider empirical distributions than EWMA predicts:

| Ticker | gate_sigma_buy | gate_sigma_sell | Reason |
|--------|---------------|-----------------|--------|
| SIVR | 1.25σ | 1.25σ | Emp P97.5 = 4.08% vs EWMA 2σ = 5.2% — gate too permissive |
| IBIT | 1.50σ | 1.50σ | Bitcoin ETF; daily moves frequently exceed EWMA predictions |
| All others | 2.00σ | 2.50σ | Global defaults |

### Vol Clamp (v2.9.4)

EWMA volatility lags true volatility during regime transitions, causing the gate to oscillate between being too strict (post-crisis: EWMA slow to fall, routine moves score as extreme) and too permissive (vol spike: EWMA slow to rise, threshold too wide).

**Fix:** `effective_vol = clamp(ewma_vol, 0.75 × rv1y, 1.50 × rv1y)`

Validated against 2019–2026 history including COVID (2020) and rate-shock (2022). Bounds are well-calibrated; ceiling binds mid-COVID (April 2020) to prevent gate lock-up at max drawdown.

### Spike-Trim Monitoring

When a position is spike-trimmed (sold into strength), the system tracks whether the same ticker is subsequently re-bought within 90 days. If the spike_trim → rebuy event rate exceeds 5% over 90 days, a `spike_trim_reentry_buffer` of 0.75% weight gap is activated (for momentum buys only). This prevents systematic sell-high/buy-higher behavior in trending assets.

### Current Gate Status (2026-03-17 close)

All pending trades are gate-clear. No z-scores approaching thresholds:

| Ticker | Direction | z-score | Threshold | Status |
|--------|-----------|---------|-----------|--------|
| VXUS | SELL | +1.75σ | 2.5σ | ✅ proceed |
| CHAT | SELL | +1.42σ | 2.5σ | ✅ proceed |
| GRID | SELL | +1.18σ | 2.5σ | ✅ proceed |
| VTI | BUY | +1.09σ | 2.0σ | ✅ proceed |
| IBIT | BUY | +0.98σ | **1.5σ** | ✅ proceed |
| SOXQ | SELL | +0.81σ | 2.5σ | ✅ proceed |
| XLE | SELL | +0.77σ | 2.5σ | ✅ proceed |
| BOTZ | SELL | +0.76σ | 2.5σ | ✅ proceed |
| SIVR | BUY | −0.18σ | **1.25σ** | ✅ proceed |
| IAUM | SELL | −0.11σ | 2.5σ | ✅ proceed |

---

## 7. Risk Controls

### Drawdown Rules

| Level | Threshold | Trigger | Action |
|-------|-----------|---------|--------|
| Soft limit | 20% peak-to-trough | Drawdown from rolling peak TPV | Freeze new buys; manual review; sell-defer max = 3 days |
| Hard limit | 28% peak-to-trough | — | Reduce all positions toward sleeve floors; min_total may be overridden |
| Recovery | <12% for 10 consecutive days | — | Resume normal rebalancing |

**Current status:** Normal. No drawdown (peak ≈ current).

### Turnover Caps

| Scope | Cap | Notes |
|-------|-----|-------|
| Per rebalance event | 20% of TPV | 22% during soft_limit |
| Annualized | 60% of TPV | ~2 major rotations/year |

When the turnover cap binds, partial rebalance is executed in order of **violation severity** (largest % cap/floor breach first).

### Rebalance Triggers

| Trigger | Condition | Notes |
|---------|-----------|-------|
| Calendar | First Monday of month | Standing rebalance |
| Band breach | Any L2 sleeve, per-ticker, or overlay band outside range for 3 consecutive days | Continuous monitoring |
| Signal drift | Sleeve > 5pp from target, or ticker > 1.5pp from target | Absolute drift, not relative |
| Stress override | soft_limit active | Suspends calendar and signal_drift; band_breach continues |

---

## 8. Pending Trades (as of 2026-03-17 close)

Not executed today. All gate-clear. Settlement: sells execute day-of, buys execute day+1 (T+1).

### Sells (execute 3/18)

| Ticker | Current Shares | Current Value | Current % TPV | Target % TPV | Delta $ | Delta Shares | Note |
|--------|---------------|--------------|--------------|-------------|---------|-------------|------|
| VXUS | 834.30 | $65,584 | 8.4% | 3.3% | −$39,552 | −503 sh | **Open: full vs partial sell** |
| DTCR | 1,776 | $44,844 | 5.7% | 1.7% | −$31,804 | −1,260 sh | |
| XBI | 315.04 | $39,124 | 5.0% | 1.0% | −$31,291 | −252 sh | |
| XLE | 849.68 | $49,706 | 6.4% | 4.4% | −$14,934 | −255 sh | ⚠️ **Min 2–3 sh TODAY to clear cap breach** |
| CHAT | 877.96 | $57,322 | 7.3% | 5.8% | −$12,168 | −186 sh | |
| SOXQ | 1,026.69 | $63,377 | 8.1% | 6.7% | −$11,234 | −182 sh | |
| REMX | 276.99 | $24,963 | 3.2% | 1.8% | −$11,079 | −123 sh | |
| BOTZ | 422.33 | $14,942 | 1.9% | 0.7% | −$9,735 | −275 sh | |
| GRID | 137 | $23,080 | 3.0% | 2.5% | −$3,544 | −21 sh | |
| IAUM | 1,047.24 | $52,184 | 6.7% | 6.2% | −$3,528 | −71 sh | |
| COPX | 279.83 | $21,801 | 2.8% | 2.7% | −$952 | −12 sh | |

### Buys (execute 3/19)

| Ticker | Current Shares | Current Value | Current % TPV | Target % TPV | Delta $ | Delta Shares |
|--------|---------------|--------------|--------------|-------------|---------|-------------|
| VTI | 286.72 | $94,781 | 12.1% | 17.5% | +$42,026 | +127 sh |
| URNM | 249.20 | $16,210 | 2.1% | 4.0% | +$15,060 | +232 sh |
| IBIT | 231 | $9,764 | 1.2% | 1.9% | +$5,441 | +129 sh |
| ITA | 188.55 | $43,913 | 5.6% | 6.1% | +$3,852 | +17 sh |
| SIVR | 574.65 | $43,248 | 5.5% | 5.8% | +$2,344 | +31 sh |

**Total sells:** ~$154,609
**Total buys:** ~$68,723
**Net turnover:** ~14.5% of TPV ✅ (within 20% cap)

---

## 9. Open Design Questions — Requesting Peer Review

These are the specific areas where independent analysis is requested. Each is listed with the current design decision and the tension or uncertainty that motivates the question.

---

### Q1 — VXUS: Band-Narrowing vs Soft Floor

**Current design:** VXUS has a [0%, 15%] per-ticker band. At 22nd percentile momentum, target = 3.3% TPV (vs current 8.4%).

**The tension:** VXUS has +4.4% alpha vs VTI over 12m. The sell is driven primarily by USD translation drag and earnings revision effects (EM exposure), not structural underperformance. At 3.3% TPV, we are drastically cutting a holding that provides international diversification and positive realized alpha.

**Two competing proposals:**
- **Band-narrowing** (Gemini's preference): Change VXUS band from [0%–15%] to [4%–12%]. Lower momentum sensitivity, keeps position structurally larger. Consistent with design — don't add ad-hoc floors, change the band.
- **Soft floor** (ChatGPT's preference): Keep [0%–15%] band but add `min_total_soft = 5.0%` for VXUS, overriding momentum mapping at the bottom. Preserves high-momentum expressiveness at the top while preventing over-selling at the bottom.

**What we want to know:**
1. Which approach is more principled for a structural core holding?
2. What historical performance evidence (2019–2026) would you expect to favor band-narrowing vs soft floor?
3. Is there a third option we haven't considered?

---

### Q2 — VIX Floor in Execution Gate

**Current design:** Vol clamp uses `effective_vol = clamp(ewma_vol, 0.75×rv1y, 1.50×rv1y)`. This provides a floor on vol to prevent the gate from becoming too permissive after a vol spike (EWMA slow to rise).

**The tension:** During rapid VIX compression (e.g., 27→23 in a single session, as observed 2026-03-17), the vol_clamp floor (0.75×rv1y) may still be too low if the VIX is compressing faster than the 252-day rolling vol can track. ChatGPT proposes:

```
effective_vol = max(ewma_vol_clamped, VIX/sqrt(252) * sqrt(2))
```

This adds an explicit VIX-derived minimum that binds during fast vol-crush sessions. Gemini considers the existing vol_clamp sufficient.

**What we want to know:**
1. Is the vol_clamp (0.75×rv1y floor) sufficient, or does VIX compression create a real blind spot?
2. If VIX floor is added, what is the expected gate fire rate increase during vol-crush windows, and is that desirable?
3. Should the VIX floor be tested specifically against 2020 COVID recovery (vol crash from 80→30), 2022 rate-shock recovery, and 2026-03-17 (VIX 27→23)?

---

### Q3 — ai_tech Dispersion-Aware Floor

**Current design:** ai_tech sleeve has a hard 22% floor. All tickers within the sleeve compete on momentum for their share of the sleeve allocation.

**The tension:** On 2026-03-17, GRID (ai_tech) is at 83rd percentile while DTCR and BOTZ are at 28th and 33rd percentile respectively. The 22% floor treats the sleeve as a monolith — when the sleeve is at floor, low-momentum tickers (DTCR, BOTZ) receive disproportionate weight relative to GRID.

**ChatGPT's proposal:** If ≥1 ai_tech ticker is above 80th percentile, allow the sleeve to sit at 24–26% rather than the hard 22% floor. This lets high-signal tickers express more weight.

**Gemini's counter:** The floor prevents total de-allocation during AI rotation. GRID's strength is already expressed through its intra-sleeve weight being maximized at its per-ticker cap. Sleeve-level floor should not expand based on individual ticker signals — that conflates L2 sleeve behavior with L1 momentum policy.

**What we want to know:**
1. Is there empirical evidence that dispersion-aware floor adjustment improves risk-adjusted return at sector inflection points (e.g., 2022 AI correction, 2023 AI recovery)?
2. Is Gemini's counter-argument correct — does intra-sleeve momentum concentration already solve the dispersion problem adequately?
3. Is there a cleaner way to handle intra-sleeve dispersion without creating a special-case rule?

---

### Q4 — DTCR Large Sell: Liquidity Risk

**Current trade:** Sell 1,260 shares of DTCR ($31,804) — a 71% reduction in position.

**The concern:** DTCR (Global X Data Center & Digital Infrastructure ETF) is a relatively thin ETF. A 1,260-share single-session sell could meaningfully impact execution price if ADV is low.

**What we want to know:**
1. What is DTCR's typical 30-day ADV (average daily volume) and what % of ADV would 1,260 shares represent?
2. Should large single-ETF sells be subject to a maximum-shares-per-session rule, and if so, what is the right threshold (e.g., 5% of 30d ADV)?
3. Is there a principled way to add ETF liquidity constraints to the policy without creating arbitrary hard rules?

---

### Q5 — VTI Large Buy: Reverse Momentum Signal

**Current trade:** Buy 127 shares of VTI (+$42,026). VTI is at 50th percentile momentum (it IS the benchmark, so by construction it ranks in the middle of the inducted universe).

**The tension:** VTI's target jumps to 17.5% TPV from 12.1% current. This buy is driven not by momentum attractiveness but by: (1) proceeds from sells needing deployment, (2) core_equity sleeve being well below its natural anchor weight. The buy is mechanically correct but could be interpreted as deploying capital into an average-momentum asset when higher-momentum alternatives exist.

**What we want to know:**
1. Is the VTI floor (10% min_total) and [10–25%] band the right anchor design for the core equity position?
2. Should proceeds from sleeve-trim sells preferentially flow to higher-momentum tickers within the same L1, rather than defaulting to VTI as the deployment vehicle?
3. Is there a more momentum-aware deployment rule that preserves the VTI anchor without mechanically over-buying it after major rotation?

---

### Q6 — Overall Signal Architecture: Are We Missing Anything?

**Current signal blend:** 45% TR12m + 35% 6m slope + 20% 3m residual vs VTI.

**The tension:** This blend is price-momentum only. No:
- Valuation overlay (e.g., sector P/E relative to historical)
- Macro regime awareness (e.g., yield curve shape, DXY level)
- Volatility regime filter (e.g., suppress momentum signals when cross-asset vol is elevated)
- Earnings revision factor

**What we want to know:**
1. For an ETF-based portfolio (no individual stocks), which additional signal layers, if any, would materially improve the signal quality?
2. Is pure price momentum the right anchor for a 10-year-horizon retirement portfolio with SEPP constraints, or should there be a structural tilt toward quality/low-vol factors?
3. How sensitive is the 45/35/20 blend to parameter choice? Is there evidence of a more optimal blend for the asset class mix in this portfolio?

---

### Q7 — Drawdown Rules: Are the Thresholds Right?

**Current thresholds:** Soft limit at 20%, hard limit at 28%, recovery at <12% for 10 consecutive days.

**The tension:**
- The soft limit (20%) is well inside a normal bear market drawdown (S&P peak-to-trough: 2022 = −25%, 2020 = −34%, 2008 = −57%).
- Recovery threshold (12%) may be too aggressive — requires 10 consecutive days below 12% drawdown, which in a volatile recovery might take months.
- Hard limit at 28% is only 8pp above soft limit. The band between "freeze new buys" and "forced reduction" is narrow.

**What we want to know:**
1. Are the soft/hard thresholds calibrated appropriately for a 15-ticker ETF portfolio (which tends to be less volatile than individual stocks)?
2. Is the 12% recovery threshold the right reentry point? Should it be defined differently (e.g., 20 consecutive days of positive momentum across the inducted universe, rather than a TPV level)?
3. The 8pp gap between soft (20%) and hard (28%) limit: is this sufficient time for manual review and action before forced liquidation begins?

---

### Q8 — SEPP Constraint Integration

**The SEPP rule:** $45,000 must be withdrawn on January 5 each year. Bucket A (TREASURY_NOTE, ~$45K) serves as the designated liquidity reserve.

**The concern:** The TREASURY_NOTE matures 12/31/2026. After maturity, Bucket A must be replenished from Bucket B. There is no explicit policy rule for how to build this position back before the next SEPP date (Jan 5, 2027).

**What we want to know:**
1. Is it appropriate to have a single fixed-income instrument (one Treasury Note) as the entire SEPP liquidity buffer?
2. Should there be a formal policy rule for TREASURY_NOTE replenishment before January 5 each year?
3. As the portfolio grows, should Bucket A grow proportionally (e.g., 1× annual SEPP) or remain fixed at $45K?

---

## 10. Portfolio Performance Context

- **Portfolio alpha YTD:** +9.68% vs S&P 500 (which is −2.07% YTD as of 2026-03-17)
- **Portfolio alpha vs Nasdaq-100 YTD:** +9.69% (QQQ is −2.08% YTD)
- **Primary drivers:** Precious metals (IAUM, SIVR), uranium (URNM), and grid infrastructure (GRID) leading

This outperformance is driven by the risk-off rotation in precious metals and real assets — exactly what the sleeve structure is designed to capture. The question is whether the pending rotation (large sells in VXUS, DTCR, XBI; large VTI buy) preserves this alpha or introduces unnecessary churn.

---

## 11. What We Are NOT Asking

Please do not comment on:
- Individual stock picks or ETF alternatives not currently in the universe
- Tax-loss harvesting (IRA — not applicable)
- Timing predictions ("the market will do X")
- Whether the SEPP amount is appropriate (legally constrained)
- System implementation details (code, automation, infrastructure)

Focus exclusively on: **policy design, signal architecture, constraint calibration, and risk rule robustness.**

---

## Appendix A: Binding Policy Files

For reference, the authoritative policy is in `mws_policy.json` (v2.9.4, last updated 2026-03-11). Key sections:
- `execution_gates` — gate parameters, per-ticker overrides, vol clamp
- `sleeves` — L1/L2 hierarchy, floors, caps
- `ticker_constraints` — per-ticker min_total/max_total, lifecycle
- `drawdown_rules` — soft/hard/recovery thresholds
- `signals` — momentum blend weights, lookback windows

## Appendix B: Previously Reviewed and Resolved

The following were reviewed over 4 rounds by ChatGPT + Gemini (declared production-ready 2026-03-10):
- Execution gate design (buy-defer, sell-defer, spike-trim)
- SIVR and IBIT per-ticker sigma overrides (v2.9.3)
- Vol clamp bounds (0.75×–1.50×rv1y) validated against 2019–2026 (v2.9.4)
- Spike-trim monitoring (90-day event counter, 5% escalation threshold)

Do not re-open these unless you have new empirical evidence.

## Appendix C: Items Logged in BACKLOG (Not Active Constraints)

These are tracked but not yet implemented:
- `vix_floor_in_execution_gate` — Q2 above
- `vxus_core_equity_downside_floor` — Q1 above
- `ai_tech_dispersion_aware_floor` — Q3 above
- `urnm_buy_gate_monitoring` — URNM buy-side gap at 1.74pp (trigger at 3pp)
- `iaum_fat_tail_monitoring` — IAUM sell-gap at 2.97pp (trigger at 3pp)
- `allocation_layer_sleeve_constraint_interaction` — mid-rank over-weighting during regime transitions

---

*End of peer review brief. Document generated 2026-03-17 from live portfolio data.*
