# MWS Market Context
<!-- Populate this file BEFORE each run. The LLM runner reads this alongside
     mws_policy.json, mws_holdings.csv, mws_tracker.json, and runner output.
     Format: fill in each section; delete placeholder lines when done.
     Leave a section blank (write "none") if nothing material in that category. -->

## Run Date
YYYY-MM-DD

## Preparation Checklist
<!-- Tick off before submitting to LLM runner -->
- [ ] Fed calendar checked (next meeting / recent minutes / speeches)
- [ ] This week's macro data releases reviewed (CPI, PCE, jobs, PMI)
- [ ] VIX level and 5-day trend noted
- [ ] Earnings calendar scanned for holdings in ai_tech / biotech sleeves
- [ ] Google News / Bloomberg headlines scanned for each category below

---

## Category: macro_rates_inflation
<!-- Fed communications, CPI/PCE/jobs data, yield curve, global central bank policy -->
<!-- Affected sleeves: core_equity, monetary_hedges, stabilizers, ALL -->

### Items
| Item | Source | Date | Materiality (HIGH/MEDIUM/LOW) |
|------|--------|------|-------------------------------|
| <!-- e.g. "Fed held rates, hawkish dot plot — median 2026 cut reduced from 3 to 2" --> | | | |

### Signal–News Interaction
<!-- For each HIGH item: confirms_momentum / contradicts_momentum / novel_not_yet_priced -->
<!-- Example: "Fed hawkish surprise CONTRADICTS recent DBMF/KMLM momentum (stabilizers elevated). No override action — momentum signal still positive and news already partially priced." -->

### LLM Notes
<!-- Free text: any nuance, combined effects, or override candidates -->

---

## Category: geopolitical
<!-- Armed conflict escalation, sanctions, trade restrictions, energy supply disruptions -->
<!-- Affected sleeves: defense_energy, strategic_materials, monetary_hedges -->

### Items
| Item | Source | Date | Materiality (HIGH/MEDIUM/LOW) |
|------|--------|------|-------------------------------|
| | | | |

### Signal–News Interaction

### LLM Notes

---

## Category: ai_tech_policy
<!-- AI regulation, chip export controls, major model releases, antitrust actions -->
<!-- Affected sleeves: ai_tech -->

### Items
| Item | Source | Date | Materiality (HIGH/MEDIUM/LOW) |
|------|--------|------|-------------------------------|
| | | | |

### Signal–News Interaction

### LLM Notes

---

## Category: energy_transition
<!-- Grid investment policy, nuclear licensing, EV adoption, carbon pricing -->
<!-- Affected sleeves: defense_energy, strategic_materials -->

### Items
| Item | Source | Date | Materiality (HIGH/MEDIUM/LOW) |
|------|--------|------|-------------------------------|
| | | | |

### Signal–News Interaction

### LLM Notes

---

## Category: crypto_regulatory
<!-- SEC/CFTC actions, ETF structure changes, exchange failures, stablecoin regulation -->
<!-- Affected sleeves: speculative (IBIT) -->

### Items
| Item | Source | Date | Materiality (HIGH/MEDIUM/LOW) |
|------|--------|------|-------------------------------|
| | | | |

### Signal–News Interaction

### LLM Notes

---

## Category: biotech_fda
<!-- FDA calendar, clinical trial readouts, drug approvals/rejections, CMS decisions -->
<!-- Affected sleeves: biotech (XBI) -->

### Items
| Item | Source | Date | Materiality (HIGH/MEDIUM/LOW) |
|------|--------|------|-------------------------------|
| | | | |

### Signal–News Interaction

### LLM Notes

---

## Category: precious_metals_macro
<!-- Real interest rates, DXY, central bank gold buying, inflation breakevens -->
<!-- Affected sleeves: monetary_hedges (IAUM, SIVR) -->

### Items
| Item | Source | Date | Materiality (HIGH/MEDIUM/LOW) |
|------|--------|------|-------------------------------|
| | | | |

### Signal–News Interaction

### LLM Notes

---

## Category: managed_futures_regime
<!-- Trend quality, cross-asset momentum, CTA positioning, correlation regime shifts -->
<!-- Affected sleeves: stabilizers (DBMF, KMLM) -->

### Items
| Item | Source | Date | Materiality (HIGH/MEDIUM/LOW) |
|------|--------|------|-------------------------------|
| | | | |

### Signal–News Interaction

### LLM Notes

---

## Portfolio-Level Override Candidates
<!-- List any specific trades where news materially changes confidence.
     The LLM will weigh these against policy constraints.
     High-materiality contradictions that may warrant discretionary_override_review. -->

| Ticker | Systematic Signal | News Direction | Materiality | Candidate Action |
|--------|------------------|----------------|-------------|-----------------|
| | | | | |

---

## VIX and Drawdown Context
<!-- Current VIX: -->
<!-- 5-day VIX trend (rising / falling / stable): -->
<!-- Current peak-to-trough drawdown from mws_tracker.json: -->
<!-- Risk regime (normal / soft_limit / hard_limit): -->

---

## LLM Runner Instructions
<!-- Do not edit this section — it is read by the LLM alongside the news above -->

1. Read `mws_policy.json → news_intelligence` for the full governance framework.
2. For each HIGH-materiality item above, explicitly state whether it CONFIRMS, CONTRADICTS, or is NOVEL relative to the current momentum signal for affected sleeves.
3. News weight cap: no single news item may shift a target weight by more than `news_intelligence.news_weight_governance.max_news_tilt_pp` percentage points beyond what systematic momentum and policy constraints already indicate.
4. All hard policy rules (hard_limit drawdown, Bucket A minimums, sleeve caps/floors, turnover caps) remain binding regardless of news.
5. Produce the full `required_recommendation_output` format including a `news_overlay_summary` section summarising which items influenced the recommendation and how.
6. If you defer any trade based on news alone (not execution gate), document it explicitly under `news_overlay_summary.override_review_candidates`.
