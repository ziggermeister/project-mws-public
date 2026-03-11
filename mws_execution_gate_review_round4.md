# MWS Execution Gate — Round 4 Review Brief
**Policy:** MWS v2.9.1 (unchanged — no policy updates until consensus)
**Prior round:** Round 3 (2026-03-10)
**Document purpose:** Resolve the single remaining divergence: spike-trim cooldown rule — add now vs monitor first.

---

## 1. What Round 3 Resolved

| Item | Status |
|---|---|
| Item C — 3-day stress sell-deferral | ✅ Both reviewers accepted. Closed. |
| Spike-trim mechanism | ✅ Both reviewers confirmed. Full mechanism logged. |
| Hidden interaction (3 mechanisms) | ✅ Confirmed and accepted. Closed. |
| Turnover attribution to originating rebalance | ✅ Both reviewers confirmed. Closed. |
| Vol conversion documentation | ✅ Both reviewers confirmed. Closed. |
| Fat-tail tickers (SIVR, IBIT) on EWMA uniform | ✅ Both reviewers accepted. Closed. |

All items are resolved except one. Round 4 is scoped exclusively to that item.

---

## 2. The Remaining Divergence

### The mechanism (agreed by both reviewers)
When a spike-trim executes at price `P_spike > P_signal`, the post-execution weight may land below the allocation target computed at signal time. At the next rebalance, if momentum rank is still high, the engine generates a small compensating buy — for the same ticker just sold into strength. This is the rank-inversion trade.

Conditions required: ticker near cap + short-term spike + momentum rank persistent. Frequency: < 1% of trades (ChatGPT estimate). Most likely tickers: SOXQ, CHAT, SIVR, URNM.

### ChatGPT's position (Round 3): Monitor first
> *"This is not necessarily harmful. Selling into spikes and occasionally rebuying slightly later often improves execution quality. The real concern is churn, not bias. The turnover cap already limits that. No policy change required yet."*
>
> Monitoring trigger: if `spike_trim → buyback within next rebalance` exceeds ~5% of spike-trim events, then consider mitigation.
>
> If mitigation ever becomes necessary, suggested rule: `spike_trim_reentry_buffer = 0.75% weight` (do not rebuy unless target − actual > 0.75%).

### Gemini's position (Round 3): Add cooldown rule now
> *"A Cooldown Rule is the safest and most robust solution. Accepting it as 'noise' means occasionally executing guaranteed-loss whipsaw trades on high-volatility assets."*
>
> Proposed rule: No momentum-driven or cap-restoration buy may be executed for an asset within N days of a spike-trim sell on that same asset.

### The core disagreement
The reviewers agree on the mechanism and that it's rare. They disagree on the threshold for action:
- ChatGPT: *rare + already bounded by turnover cap = acceptable; add complexity only when empirically triggered*
- Gemini: *any known whipsaw loop = should be closed proactively, regardless of frequency*

---

## 3. Questions for Both Reviewers

### For Gemini
ChatGPT's full mechanism analysis (Round 3) provided two points not present in Round 2 when you recommended the cooldown:

**Point 1 — Directionality:** The rank-inversion rebuy is not a guaranteed loss. Selling into a spike and rebuying slightly lower is often a profitable sequence. The "whipsaw" framing may overstate the cost — a spike-trim sell at `P_spike` followed by a rebuy at `P_T1` is harmful only if `P_T1 > P_spike`, which requires the spike to persist without reversion. How often that occurs in high-vol tickers (SOXQ, SIVR, URNM) relative to mean-reversion frequency is an empirical question.

**Point 2 — Turnover cap adequacy:** The existing 20% turnover cap per rebalance event clips the rebuy anyway. A spike-trim sell followed by a same-ticker rebuy within one cycle effectively consumes two slots of the turnover budget for a round-trip — a self-limiting structure.

Given these two points, does Gemini still prefer a hard cooldown rule upfront? Or is a monitoring-only approach (with a specific escalation trigger) acceptable?

**Specific ask:** If monitoring-only is not acceptable, please specify:
- N (how many calendar days for the cooldown)
- Scope (momentum buys only, or also cap-restoration buys)
- Whether the rule should be a hard block or a soft warning (log only)

### For ChatGPT
Your monitoring-first approach requires a defined escalation trigger and a clear mitigation if triggered. You suggested `spike_trim_reentry_buffer = 0.75% weight` as the eventual mitigation.

**Specific ask 1:** How should the monitoring be implemented in the policy? Options:
- (a) Log-only: engine flags each spike_trim → same-ticker-rebuy event. Human reviews monthly.
- (b) Automatic counter: policy defines a rolling 90-day counter; if > 5% of spike-trim events generate a same-ticker rebuy within 1 rebalance cycle, engine auto-activates the `spike_trim_reentry_buffer`.
- (c) Manual trigger only: no automatic escalation. Policy notes the risk; mitigation is discretionary.

**Specific ask 2:** The `spike_trim_reentry_buffer = 0.75%` weight threshold you proposed — how does it interact with the existing `min_total` floor enforcement? If a ticker is at, say, 7.5% actual vs 8.0% target (0.5% gap), the reentry buffer would block the buy. But the floor enforcer also expects the ticker to be at min_total. Does the reentry buffer take precedence over cap-floor compliance enforcement, or only over momentum-driven buys?

---

## 4. Two Possible Resolution Paths

We are proposing two paths and asking reviewers to endorse one:

### Path A — Monitor only (ChatGPT's preference)
No new rule added. Policy notes the mechanism and defines a concrete monitoring obligation:
```json
"spike_trim_cap_interaction": {
  "status": "accepted_with_monitoring",
  "mechanism": "Cap-boundary oscillation: spike-trim execution at P_spike > P_signal may cause post-trim weight to undershoot target, triggering a compensating buy at next rebalance on the same ticker.",
  "frequency_estimate": "< 1% of trades. Conditions: ticker near cap + spike + persistent rank.",
  "monitoring": {
    "log_event": "spike_trim_followed_by_same_ticker_rebuy_within_1_rebalance",
    "escalation_trigger": "event_rate > 5% of spike_trim_events over rolling 90 days",
    "escalation_action": "activate spike_trim_reentry_buffer"
  },
  "contingent_mitigation": {
    "rule": "spike_trim_reentry_buffer",
    "value": "0.75%_weight",
    "activation": "manual_or_auto_at_escalation_trigger",
    "scope": "momentum_buys_only",
    "does_not_override": "cap_floor_compliance_enforcement"
  }
}
```

### Path B — Cooldown rule now (Gemini's preference)
A lightweight, bounded cooldown rule is added immediately:
```json
"spike_trim_cap_interaction": {
  "status": "mitigated",
  "mechanism": "Cap-boundary oscillation: spike-trim execution at P_spike > P_signal may cause post-trim weight to undershoot target, triggering a compensating buy at next rebalance on the same ticker.",
  "cooldown_rule": {
    "enabled": true,
    "trigger": "spike_trim_executed_for_ticker",
    "block": "momentum_driven_buys_only",
    "does_not_block": "cap_floor_compliance_enforcement",
    "cooldown_calendar_days": 5,
    "rationale": "5 days covers one typical rebalance cycle gap. Does not block hard compliance enforcement. Momentum buys can resume after cooldown."
  }
}
```

Note on Path B: `cooldown_calendar_days` is set to 5 as a draft — reviewers should specify preferred value.

---

## 5. Our Lean

We have no strong view. The mechanism is confirmed, the frequency is low, and the turnover cap provides a partial natural limit. Both paths are defensible. We are waiting for reviewer convergence before writing anything to the policy file.

If reviewers cannot converge after Round 4, we will implement Path A (monitor only) as the lower-complexity default and treat mitigation as a Phase 2 decision, consistent with the general design philosophy of not adding rules ahead of empirical need.

---

## 6. Reviewer Checklist for Round 4

- [ ] **Gemini:** Given ChatGPT's directionality and turnover-cap-adequacy points, do you still require a hard cooldown rule upfront? Or is Path A (monitor with defined escalation) acceptable?
- [ ] **ChatGPT:** How should monitoring be implemented — log-only, automatic counter, or manual trigger only? Does `spike_trim_reentry_buffer = 0.75%` take precedence over cap-floor compliance, or only over momentum buys?
- [ ] **Both:** Which path do you endorse — Path A (monitor) or Path B (cooldown)? If Path B, what value for `cooldown_calendar_days`?

---

*Round 4 brief generated 2026-03-10. Policy file is at v2.9.1 — unchanged pending Round 4 consensus.*
