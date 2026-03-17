# MWS Peer Review — Breadth-Conditioned ai_tech Floor
**Version:** MWS v2.9.5 → proposed v2.9.6
**Date:** 2026-03-17
**Scope:** Single mechanism proposal — conditional floor for the ai_tech sleeve

---

## Background

This review is the output of a prior design review where two LLM reviewers (ChatGPT and Gemini) independently identified an intra-sleeve dilution problem in the ai_tech sleeve. Both converged on the same proposed fix: a breadth-conditioned floor. We are now asking for a focused review of the proposed rule before implementing it.

This review is about **one specific mechanism**. Do not evaluate the rest of the system.

---

## The Current Design

### ai_tech Sleeve (Current)

| Parameter | Value |
|-----------|-------|
| L1 parent | `growth` (60% cap) |
| L2 floor | **22%** of allocatable denominator (static) |
| L2 cap | 32% |
| Tickers | SOXQ, CHAT, BOTZ, DTCR, GRID |

### Per-Ticker Caps (% of TPV)
| Ticker | Max |
|--------|-----|
| SOXQ | 10% |
| CHAT | 8% |
| DTCR | 6% |
| GRID | 5% |
| BOTZ | 2% |

None of these tickers have a per-ticker **floor** (min_total). Only the sleeve has a floor.

### How the Floor Actually Behaves

This is critical context. The 22% floor is **not a rebalancing floor**. It does not force buying to maintain 22% at all times.

From the policy:

```json
"floor_behavior": "hold_at_floor_if_positive_momentum"
"floor_exit_behavior": "reduce_to_zero_if_blend_negative_for_N_days"
"floor_exit_days": 20
```

And the sleeve floor validator:
```
severity: "warning"
soft constraint — relaxed proportionally if infeasible
```

This means:
- Individual tickers exit to zero after 20 consecutive days of negative momentum blend
- When all tickers in a sleeve exit, the sleeve floor becomes infeasible and is automatically released
- The system does NOT sell other assets to rebuy a falling AI sleeve
- The floor is a minimum holding level **when momentum is positive and capital is available**

A "death spiral" scenario (sell gold to rebuy crashing semis) cannot happen in this design.

### The Problem That Was Identified

The floor problem is narrower than originally thought. It occurs in **mixed-signal regimes**, not crash scenarios.

**Example:**
- GRID: 83rd percentile (strong)
- SOXQ: 45th percentile (mediocre)
- DTCR: 38th percentile (weak)
- CHAT: 30th percentile (weak)
- BOTZ: 15th percentile (very weak)

In this regime:
1. GRID gets maximum allocation → hits its 5% cap quickly
2. The sleeve still needs 22% of the denominator total
3. Remaining ~17% must go somewhere inside ai_tech
4. System allocates to SOXQ, DTCR, CHAT, BOTZ — weak names — because they are the only remaining members of the sleeve with positive (or near-positive) momentum
5. Result: capital is forced into weak AI sub-components not because of conviction, but because of sleeve arithmetic

**This is the specific failure mode being solved.** It does not involve crash scenarios — it is a structural inefficiency in normal mixed regimes.

### The Investor's Intent

The 22% floor reflects a long-term structural conviction: AI/semiconductors is the dominant theme in the portfolio over a 10-year horizon. The floor is designed to prevent premature abandonment of the thesis during normal corrections.

The investor **does not** want to remove the floor. The investor **does** want to fix the dilution problem.

---

## The Proposed Fix: Breadth-Conditioned Floor

Replace the static 22% floor with a floor that scales based on the number of ai_tech tickers with positive momentum breadth.

### Proposed Rule

```
positive_count = number of ai_tech tickers where blend_score > 0

if positive_count >= 3 (maintained for >= 5 consecutive days):
    ai_tech floor = 22%

if positive_count <= 2 (maintained for >= 5 consecutive days):
    ai_tech floor = 12%

if positive_count == 0 OR >= 4 of 5 tickers in floor_exit state:
    ai_tech floor = 0% (infeasible → auto-released per existing soft-floor rule)
```

### Design Rationale

| Component | Rationale |
|-----------|-----------|
| 22% strong breadth floor | Preserves full AI conviction when sector is broadly healthy |
| 12% weak breadth floor | Maintains structural exposure (one strong name can fill this) without forcing dilution into weak names |
| 5-day hysteresis | Prevents the floor from bouncing when a marginal ticker oscillates around zero; mirrors the logic of the existing exit rule (20-day persistence) and re-entry rule (15-day persistence) |
| `blend_score > 0` as "positive" | Consistent with existing exit rule definition; absolute positive momentum, not relative rank |
| 0% / infeasible state | Already handled by existing soft-floor mechanic — no change required |

### Why 12% as the Weak-Breadth Floor?

12% is approximately the sum of two strong AI names at or near their per-ticker caps:
- SOXQ at ~10% TPV (adjusted to allocatable denom basis) ≈ ~12%
- GRID at ~5% TPV ≈ ~6%

When only 1–2 AI names are working, a 12% sleeve target means the system can fill it with the genuinely strong names and not be forced into the weak ones.

---

## Questions for Review

### 1. Is the binary structure correct (22% / 12%), or should there be a graduated scale?

ChatGPT's original suggestion was a three-tier gradient (22% / 16-18% / 10-12% / 0%). We simplified to binary (22% / 12%) for ease of implementation and auditability. Is binary too coarse? What does the graduated approach give you that binary doesn't, and is it worth the added complexity for a single-operator system?

### 2. Is 5 days the right hysteresis window?

The 5-day requirement means a floor transition only triggers after a full trading week of consistent breadth. Is this too short (floor oscillates), too long (floor lags the actual regime), or about right? How sensitive is this to the existing 20-day exit and 15-day re-entry thresholds?

### 3. Is `blend_score > 0` the right definition of "positive"?

An alternative is above 50th percentile within the inducted universe. That means a ticker only counts as "positive" if it's above the median of all 18 tickers, not just above zero. Does this create better signal quality, or does it conflate "AI is weaker than other sleeves" with "AI is weak in absolute terms"?

### 4. Is 12% the right weak-breadth floor?

The 12% fallback was derived from the sum of per-ticker caps for the two strongest AI names. Is there a better way to calibrate this — e.g., as a function of the number of positive tickers (positive_count × average_cap), or as a fixed fraction of the full floor (50% of 22% ≈ 11%)?

### 5. Does this rule create new failure modes we haven't anticipated?

Specifically:
- Does the 5-day hysteresis create a window during which the portfolio is misallocated in a fast-moving regime?
- Does the floor transition (22% → 12%) itself trigger a rebalance event large enough to consume the 20% turnover cap?
- Is there a scenario where the breadth-conditioned floor and the existing drawdown rules interact badly?

### 6. Should the same breadth-conditioning logic apply to other sleeves?

ai_tech is the only sleeve with this treatment. Other multi-ticker sleeves: `core_equity` (VTI + VXUS), `real_assets` (strategic_materials + defense_energy), `precious_metals` (IAUM + SIVR). Should any of these get a breadth-conditioned floor, or is ai_tech structurally unique (high conviction, high dispersion, high-beta)?

---

## What We Are NOT Asking

- Do not re-evaluate the 22% floor level itself — the investor's AI conviction is confirmed and not in scope
- Do not propose removing the floor entirely
- Do not propose changes to other sleeves unless you believe the breadth logic must be consistent across all of them to be coherent
- Do not propose changes to the momentum signal, execution gate, or rebalance triggers
- Do not evaluate the managed futures overlay or drawdown controls

---

## How to Structure Your Response

1. **Is the binary structure right?** 22% / 12% vs. graduated — give a direct recommendation with reasoning.
2. **Parameter assessment** — hysteresis window, "positive" definition, 12% floor level. For each: keep as proposed, or change to X with this rationale.
3. **Failure modes** — identify any you see that we haven't anticipated.
4. **Other sleeves** — yes or no on applying breadth conditioning elsewhere, and why.
5. **Overall verdict** — implement as proposed, implement with modifications (specify), or don't implement (explain why).

---

*MWS v2.9.5 | TPV ~$780K | IRA SEPP | ai_tech sleeve only | 2026-03-17*
