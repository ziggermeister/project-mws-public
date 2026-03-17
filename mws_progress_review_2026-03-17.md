# MWS Progress Review — Post-Implementation Check-In
**Date:** 2026-03-17
**Context:** Follow-up to the open design review brief sent earlier today

---

## What This Is

You reviewed the MWS system design earlier today in response to a brief that asked: *"If no code were written yet, what would you change before writing the first line?"*

That review produced several high-priority recommendations. We have now implemented the most critical ones. This brief summarizes what was changed and asks: **given where the system stands now, what gaps remain?**

We are not asking you to validate the implementation. We are asking whether the changes we made addressed the real problems, and whether implementing them has revealed new gaps we should address next.

---

## What Was There Before (Summary)

A systematic momentum-weighted portfolio system for a ~$780K IRA with mandatory $45K annual SEPP withdrawals. Key prior design:

- Momentum blend (45% TR12m / 35% 6m slope / 20% 3m residual vs VTI), percentile-ranked within 18 inducted ETFs
- Two-level sleeve hierarchy (L1 caps, L2 floors/caps) denominated off allocatable capital (TPV minus overlays minus Bucket A)
- Bucket A: $45K Treasury Note reserved for the annual SEPP withdrawal, untouchable
- Managed futures overlay (DBMF + KMLM, 6–12% TPV) excluded from allocatable denominator
- Execution gate: EWMA vol-scaled z-score, defers buys into spikes and sells into capitulation
- Hard drawdown controls: soft limit 22%, hard limit 30%
- LLM reads pre-computed analytics and applies policy to produce trade recommendations

---

## What Has Been Implemented Since the Design Review

### v2.9.5 — Five structural fixes (implemented before the design review brief)
1. VXUS floor added (4% min) — international diversification enforced structurally
2. GRID cap raised 3% → 5% — allows momentum to express modest AI infrastructure conviction
3. Drawdown thresholds widened: soft 20% → 22%, hard 28% → 30%, recovery <12% → <15% OR VTI 5-day positive momentum
4. Turnover cap exemption: hard-limit compliance trades bypass the 20% per-event turnover cap
5. Residual allocation rule: sell proceeds deploy to underweight high-momentum tickers before VTI

### v2.9.6 — Breadth-conditioned ai_tech floor
Replaced static 22% ai_tech sleeve floor with a breadth-conditioned floor:
- ≥3 of 5 ai_tech tickers with positive blend score for ≥5 consecutive days → floor = 22%
- <3 positive → floor = 12%
- 0 positive or ≥4 in exit state → floor = 0% (infeasible, auto-released)

**Rationale addressed:** When only 1–2 ai_tech tickers were genuinely strong, the static 22% floor forced capital into weaker sub-components to satisfy sleeve arithmetic. Breadth-conditioned floor preserves full AI conviction when the sector is broadly healthy without forcing intra-sleeve dilution in mixed-signal regimes.

**What was NOT changed:** The AI conviction thesis (floor exists for a reason) and the 22% level (intentional long-term commitment). The floor mechanics, not the conviction, were refined.

Persistent hysteresis state written to `mws_breadth_state.json` by `mws_analytics.py` each run. Runner reads persisted state so floor transitions are stable across rebalance events.

### v2.9.7 — Absolute momentum filter
Blocks new capital deployment to any ticker with `blend_score ≤ 0`, regardless of relative percentile rank:
- Momentum buys blocked if raw blend score is negative
- Residual cash deployment blocked if raw blend score is negative
- Compliance buys, trims, and spike trims are exempt — structural enforcement is unaffected

**Rationale addressed:** Pure relative ranking always produces buy signals. In a broad risk-off environment where all tickers have negative momentum, the system would previously still route new capital to the "least bad" name. The filter allows the system to go quiet (no momentum buys, residual stays in cash) when opportunity quality is broadly poor.

---

## What Was Explicitly Decided Against

- **Removing the ai_tech floor:** Investor has long-term structural conviction in AI/semiconductors as the primary alpha driver. Floor is intentional. Breadth-conditioning addresses the mechanical dilution problem without abandoning the thesis.
- **Moving all logic out of the LLM:** LLM reasoning, news synthesis, and edge-case judgment are intentional features. Python owns all quantitative arithmetic; LLM reads pre-computed outputs and adds qualitative overlay. This separation is correct.
- **ADV execution constraint:** At ~$780K portfolio size, no trade would approach 5% of 30-day ADV for any ETF in the universe. Not applicable at this scale.

---

## What Is Still Pending

**SEPP replenishment rule (deferred):** No formal policy exists for pre-positioning the $45K annual withdrawal. The current Treasury Note matures December 2026. There is no automatic sweep of dividends to Bucket A, no forced fractional sells if Bucket A is underfunded in the months before the maturity. This is a known gap — deferred to a separate policy design session.

---

## What We Are Asking

Given the changes above:

1. **Did we fix the right things?** The two highest-priority recommendations from the design review were the absolute momentum filter and the ai_tech floor mechanics. We addressed both. Did we solve the real problem, or did we solve the surface symptom while leaving the root cause intact?

2. **What new gaps did implementation reveal?** The absolute momentum filter means the system can now hold cash in a broad risk-off environment. Does this create a new problem — e.g., how does cash interact with sleeve floors? If cash is included in the allocatable denominator, a large cash position compresses sleeve allocations toward their floors even when the system is intentionally not deploying. Is this the right behavior?

3. **Is the breadth-conditioned floor the right mechanism, or did we over-engineer a symptom?** The intra-sleeve dilution problem arose because individual ticker caps prevented capital from concentrating in the strong name. An alternative fix would have been to raise individual ticker caps (let the winner run). Is the breadth floor the right fix or did we solve it at the wrong layer?

4. **What is the most important remaining gap?** Given what has been implemented, and the SEPP replenishment rule still pending — what is the single highest-impact thing not yet addressed?

5. **How should the system behave when the absolute momentum filter blocks all momentum buys?** Cash builds. Sleeve weights drift. At some point the cash position itself becomes a compliance issue (it's included in the denominator, compressing all sleeve percentages). Is there a rule needed for "cash exceeds X% of portfolio → automatically redeploy to VTI regardless of momentum filter"? Or is this already handled by the sleeve floor compliance mechanic?

---

## Response Format

1. **Did we fix the right things?** Direct answer — yes/no/partially, with explanation.
2. **New gaps from implementation** — specifically the cash/floor interaction question.
3. **Breadth floor vs cap expansion** — which layer was the right fix?
4. **Single highest-impact remaining gap** — one item, ranked by expected impact on 10-year outcome.
5. **Cash accumulation rule** — should there be one? If yes, propose the specific rule.

Be direct. If something we implemented is wrong or incomplete, say so.

---

*MWS v2.9.7 | ~$780K IRA SEPP | 2026-03-17*
