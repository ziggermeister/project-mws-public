# MWS Policy & Implementation Sign-Off — v2.9.9
**Date:** 2026-03-17
**Purpose:** Final review before production run. All mechanisms implemented. Requesting go/no-go.

---

## What This Is

You have reviewed MWS in several rounds today. This is the final sign-off request. We are asking you to review the complete, exact policy and runner specifications as implemented — not as described — and confirm whether the system is coherent and ready to run, or identify any remaining blocking issue.

We are not asking for new suggestions. We are asking: **given exactly what is here, is there anything that would cause a material failure in a live portfolio run?**

---

## Changes Implemented Since Your Last Review

### v2.9.6 — Breadth-conditioned ai_tech floor
- Static 22% floor replaced with breadth-conditioned floor
- Strong breadth (≥3 of 5 tickers with `RawScore > 0`, sustained ≥5 days): floor = 22%
- Weak breadth (<3 positive, sustained ≥5 days): floor = 12%
- Infeasible (0 positive OR ≥4 in floor_exit): floor = 0%, auto-released
- 5-day hysteresis tracked in `mws_breadth_state.json`

### v2.9.7 — Absolute momentum filter
- Momentum buys blocked when `Pct ≥ 0.65` AND `RawScore ≤ 0`
- Compliance buys, trims, and spike trims are exempt
- Blocked buys recorded as `basis: "hold|abs_filter"`
- Residual cash deployment also filtered (only positive-blend tickers receive residual)

### v2.9.8 — Bifurcated denominators + tactical cash
- `sizing_denom = TPV − overlays − Bucket A` (unchanged; used for all target $ sizing)
- `compliance_denom = sizing_denom − tactical_cash` (used only for floor/cap breach detection)
- Tactical cash activates when: filter blocking ≥2 consecutive trading days AND `cash > 1% TPV buffer`; capped at 30% TPV
- Tracked in `mws_tactical_cash_state.json` via `mws_analytics.py`

### v2.9.9 — Compliance buy turnover cap
- Regular L2 compliance buys (Priority 3) now subject to 20% per-event turnover cap
- `comp_buy_scale = min(1.0, available_cash/need, turnover_cap_usd/need)`
- Unfilled deficit queues to next rebalance cycle automatically
- Priority 1 (hard_limit) compliance trades remain turnover-cap-exempt
- Display note identifies whether scaling was turnover-bound or cash-bound

---

## Exact Policy Specifications

### Execution Priority Hierarchy
| Priority | Trigger | Action | Turnover Cap |
|---|---|---|---|
| 1 | hard_limit breach | Reduce all to sleeve floors | **Exempt** |
| 2 | Bucket A below $45K | Halt buys, restore Bucket A | — |
| 3 | L1/L2 cap/floor breach, breadth floor transition | Enforce compliance sells first, then compliance buys | **Subject to 20% cap** (v2.9.9) |
| 4 | Turnover limit | Clip remaining rebalance | — |
| 5 | Signal drift / band breach / calendar | Execute momentum-driven rebalance | Subject to 20% cap |

### Denominator Rules
| Denominator | Formula | Used For |
|---|---|---|
| `sizing_denom` | TPV − overlays − Bucket A | All target $ calculations |
| `compliance_denom` | `sizing_denom` − tactical_cash | L1/L2 floor/cap breach checks only |

### Tactical Cash Activation
```
filter_blocking = any ticker: Pct ≥ 0.65 AND RawScore ≤ 0
raw_tactical_cash = max(0, cash_balance − 1% TPV)
tactical_cash = min(raw_tactical_cash, 30% TPV)
    IF: filter_blocking TRUE for ≥ 2 consecutive days AND raw_tactical_cash > 0
    ELSE: 0
```

### Momentum Filter
```
IF ticker.Pct ≥ 0.65 AND ticker.RawScore ≤ 0:
    action = HOLD (basis: "hold|abs_filter")
    → buy suppressed; compliance buys exempt
```

### ai_tech Breadth Floor
```
positive_count = |{SOXQ, CHAT, BOTZ, DTCR, GRID} where RawScore > 0|

floor = 22%  if positive_count ≥ 3, sustained ≥ 5 trading days
floor = 12%  if positive_count < 3, sustained ≥ 5 trading days
floor = 0%   if positive_count == 0 OR floor_exit_count ≥ 4 (infeasible → released)
```

### Compliance Buy Scaling (v2.9.9)
```
turnover_cap_usd = total_val × 0.20
comp_buy_scale = min(1.0,
                     total_available / comp_buy_need,
                     turnover_cap_usd / comp_buy_need)
```

---

## State Files Written by mws_analytics.py

| File | Updated | Contents |
|---|---|---|
| `mws_breadth_state.json` | Daily, after `generate_rankings()` | Per-sleeve: current_category, pending_category, pending_days, last_date, effective_floor |
| `mws_tactical_cash_state.json` | Daily, after `generate_rankings()` | filter_blocking (bool), consecutive_blocked_days (int), date |

Both files are read by `mws_runner.py` at report time. Missing or corrupt files fall back to conservative defaults (full floor, filter inactive).

---

## What We Are Asking

For each of the following, give a one-line verdict: **GO** or **BLOCKING ISSUE [description]**.

1. **Priority hierarchy** — Is the execution order (Priority 1–5) internally consistent? Does the compliance buy cap (v2.9.9) interact correctly with the existing Priority 4 turnover clip?

2. **Denominator bifurcation** — Is the sizing_denom / compliance_denom split correctly scoped? Any scenario where the wrong denominator is used for the wrong purpose?

3. **Tactical cash state machine** — Is the 2-day persistence + 30% cap + 1% buffer sufficient to prevent false activation? Any scenario where tactical cash activates incorrectly?

4. **Compliance buy queuing** — When compliance buys are turnover-capped and the deficit queues to the next cycle, does the system re-detect the floor breach naturally without requiring special state tracking?

5. **Recovery sequencing** — When the filter lifts: (a) tactical_cash → 0, (b) compliance_denom snaps back, (c) floor deficits open, (d) compliance buys fire at ≤20% — is this sequence coherent? Does the ai_tech breadth floor restore correctly (needs 5 days)?

6. **Overall system** — GO / NO-GO for a live portfolio run.

---

## Response Format

One line per question: **GO** or **BLOCKING ISSUE: [one sentence describing what would fail and why]**.

If NO-GO on question 6: specify the single highest-priority fix required before running.

---

*MWS v2.9.9 | TPV ~$780K | IRA SEPP | Final production sign-off | 2026-03-17*
