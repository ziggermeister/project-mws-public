# MWS Mechanism Interaction Review — v2.9.6 / v2.9.7 / v2.9.8
**Date:** 2026-03-17
**Scope:** Three recently implemented mechanisms evaluated as a combined system

---

## Context

Three major mechanisms were implemented today based on peer review feedback. Each was reviewed and approved individually. This brief asks you to evaluate them **as a combined system** — specifically whether they interact correctly across regimes, whether their priority/sequencing is right, and whether implementing all three together has created any edge cases that weren't visible when reviewing them one at a time.

Do not re-evaluate whether each mechanism is a good idea. They are implemented and will not be rolled back. Evaluate whether they work correctly *together*.

---

## The Three Mechanisms

### Mechanism 1 — Breadth-Conditioned ai_tech Floor (v2.9.6)

The ai_tech sleeve floor is no longer static. It responds to how many of the 5 ai_tech tickers have positive absolute momentum (`RawScore > 0`):

```
positive_count = tickers in {SOXQ, CHAT, BOTZ, DTCR, GRID} where RawScore > 0

if positive_count >= 3 (sustained >= 5 consecutive trading days):
    ai_tech floor = 22%

if positive_count < 3 (sustained >= 5 days):
    ai_tech floor = 12%

if positive_count == 0 OR >= 4 tickers in floor_exit state:
    ai_tech floor = 0% (infeasible → auto-released)
```

**Hysteresis:** The floor only transitions after the new breadth condition has held for 5 consecutive trading days. A separate state file (`mws_breadth_state.json`) tracks this with pending/confirmed counters per sleeve.

**Priority:** Breadth-triggered floor reductions are classified as Priority 3 (cap/floor compliance), executing before momentum signal trades.

---

### Mechanism 2 — Absolute Momentum Filter (v2.9.7)

Before any momentum buy executes, the system checks whether the ticker has positive absolute momentum:

```
if ticker.Pct >= 0.65 AND ticker.RawScore <= 0:
    action = HOLD (basis: "hold|abs_filter")
    → momentum buy is suppressed
```

**What is exempt:** Compliance buys (floor enforcement), momentum trims, spike trims. Only momentum buys are affected.

**Effect:** In a broad risk-off environment where all or most tickers have negative RawScore, the system stops buying and cash accumulates naturally.

---

### Mechanism 3 — Bifurcated Denominators + Tactical Cash (v2.9.8)

When the absolute momentum filter is actively suppressing buys and cash builds as a result, the system bifurcates its denominator:

```
sizing_denom     = TPV - overlays - Bucket A
                   ← used for ALL target $ calculations
                   ← cash stays visible; recovery buys scale correctly

compliance_denom = sizing_denom - tactical_cash
                   ← used ONLY for L1/L2 floor/cap breach detection

tactical_cash = max(0, cash_balance - 1% TPV buffer)
    WHEN: filter_blocking is TRUE for >= 2 consecutive trading days
    CAP:  30% of TPV maximum exclusion
    ELSE: 0

filter_blocking = any ticker has Pct >= 0.65 AND RawScore <= 0
```

**State file:** `mws_tactical_cash_state.json` — tracks `filter_blocking` and `consecutive_blocked_days`, updated daily by `mws_analytics.py`.

---

## How They Are Designed to Interact

In a broad bear market:
1. RawScore falls negative for most tickers
2. **Mechanism 2** blocks momentum buys → cash builds
3. **Mechanism 3** detects `filter_blocking` after 2 days → classifies excess cash as tactical → shrinks `compliance_denom`
4. Sleeve floors, measured against smaller `compliance_denom`, are satisfied by remaining invested capital → no spurious compliance buys
5. **Mechanism 1** reads falling positive_count in ai_tech → after 5 days, floor relaxes 22% → 12% → system needs less capital in ai_tech, reducing any remaining pressure

In recovery:
1. RawScore turns positive
2. **Mechanism 2** filter lifts → buys allowed
3. **Mechanism 3** `filter_blocking` → false → `tactical_cash` → 0 → `compliance_denom` = `sizing_denom` in one cycle
4. Full denominator restored → sleeve floors assessed at full scale → potential wave of recovery buys, governed by 20% turnover cap
5. **Mechanism 1** reads rising positive_count → after 5 days, ai_tech floor restores 12% → 22%

---

## Questions for Review

### 1. Do these three mechanisms interact correctly in a sustained bear market?

Walk through this scenario:
- Months 1–2: All 18 tickers have negative RawScore
- Mechanism 2 blocks all momentum buys from day 1
- Cash builds from 3% to 35% over 8 weeks
- Mechanism 3 activates after day 2 — compliance_denom shrinks with cash
- Mechanism 1: all 5 ai_tech tickers negative → after 5 days, floor drops to 12%; after 20 days each, tickers individually exit to zero → floor eventually becomes infeasible (0%)

**Question:** Does the system reach a stable, coherent state in this scenario? Are floors satisfied, no forced compliance buys, and cash correctly classified throughout?

### 2. The recovery burst — does the turnover cap create a problem?

When the filter lifts after a sustained bear:
- `compliance_denom` snaps back to `sizing_denom` in one cycle (large denominator jump)
- All previously exited tickers may qualify for re-entry simultaneously (15-day positive streak + VIX < 28)
- Breadth floor hasn't yet transitioned (needs 5 days), so ai_tech floor is still at 12%
- Potential wave of recovery buys hitting the 20% per-event turnover cap

**Question:** Does the 20% turnover cap adequately govern this re-entry burst, or does the denominator snap-back create a compliance floor breach that is exempt from the cap (Priority 3) and forces a massive compliance buy regardless?

### 3. Boundary condition — exactly at the breadth threshold

When `positive_count` is exactly 3 (the strong/weak boundary):
- One marginal ticker oscillates around RawScore = 0 daily
- The 5-day hysteresis should prevent the floor from bouncing
- But the absolute momentum filter uses `RawScore <= 0` as its threshold (same signal, no hysteresis)

**Question:** Is there a regime where the breadth hysteresis (5-day) and the filter activation (no hysteresis) create a contradiction — the floor is in "strong" state (22%) because breadth hasn't yet transitioned, but buys are blocked by the filter, and compliance_denom is shrunk? Does this create a compliance buy signal for ai_tech at 22% floor level that immediately gets blocked by the filter?

### 4. Hysteresis clock collision

Both mechanisms use state files with day-counters:
- `mws_breadth_state.json` — tracks pending_days and current_category per sleeve (5-day threshold)
- `mws_tactical_cash_state.json` — tracks consecutive_blocked_days (2-day threshold)

Both are written by `mws_analytics.py` and read by `mws_runner.py`.

**Question:** Is there a scenario where both state machines are mid-transition simultaneously — breadth is in a 5-day pending period transitioning 22%→12%, and tactical cash is in its 2-day activation window — and the system sees inconsistent signals between what the floor expects and what the denominator provides?

### 5. Priority conflict — who wins when Mechanism 1 and Mechanism 2 disagree?

Scenario: ai_tech breadth is "strong" (22% floor active, not yet transitioned). One ai_tech ticker (GRID) has Pct >= 0.65 but RawScore = -0.02 (barely negative).

- Mechanism 1 says: "ai_tech is healthy, enforce 22% floor"
- Mechanism 2 says: "GRID has negative absolute momentum, block its buy"
- If GRID is the only ticker creating a floor deficit, the system detects a compliance breach (Priority 3) but the abs filter is only for momentum buys — compliance buys are exempt

**Question:** Does a compliance buy for GRID execute correctly (because compliance buys are filter-exempt), and is this the intended behavior? Or should compliance buys also respect the absolute momentum filter in some cases?

---

## What We Are NOT Asking

- Do not re-evaluate whether these mechanisms are good ideas
- Do not propose structural alternatives
- Do not evaluate other parts of the system (execution gate, drawdown rules, ticker universe)
- Answer only the 5 questions above — one clear verdict per question with specific reasoning

---

## Response Format

For each question (1–5):
- **Verdict:** [Correct / Incorrect / Needs modification]
- **Reasoning:** 2–4 sentences max
- **If incorrect or needs modification:** Exact proposed fix (one sentence, actionable)

---

*MWS v2.9.8 | TPV ~$780K | IRA SEPP | Mechanism interaction review only | 2026-03-17*
