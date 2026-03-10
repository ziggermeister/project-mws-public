# Momentum-Weighted Scaling (MWS) v2.8.5

A systematic, rules-based portfolio management system for a personal investment account with annual SEPP withdrawals. Combines momentum signals with strict sleeve caps/floors to outperform VTI by ≥150bps net of turnover costs over a 10-year horizon.

---

## ⚠️ Authority & Precedence

> **`mws_policy.json` is the single authoritative source of truth.**
> `mws_macro.md` is advisory/explanatory only.
> Execution code is authoritative over prose.
> Any conflict: policy JSON wins.

---

## File Index

### Authoritative
| File | Role |
|------|------|
| [`mws_policy.json`](mws_policy.json) | **Binding rule set** — caps, floors, signals, constraints, lifecycle rules |
| [`mws_policy_contract.py`](mws_policy_contract.py) | Policy validation code |
| [`mws_macro.md`](mws_macro.md) | Explanatory governance doc (advisory only) |

### Data
| File | Role |
|------|------|
| [`mws_holdings.csv`](mws_holdings.csv) | Current holdings (ticker, shares, class) |
| [`mws_tracker.json`](mws_tracker.json) | State tracker — inducted universe, signals, peak TPV |
| [`mws_ticker_history.csv`](mws_ticker_history.csv) | Historical price data (Date, Ticker, AdjClose) |
| [`mws_recent_performance.csv`](mws_recent_performance.csv) | Daily performance log with TWR and benchmark comparison |
| [`mws_run_results.csv`](mws_run_results.csv) | Run output log |

### Execution
| File | Role |
|------|------|
| [`mws_titanium_runner.py`](mws_titanium_runner.py) | Python engine — price fetch, momentum calc, rebalance |
| [`daily-emailer/DailyEmailRunner.js`](daily-emailer/DailyEmailRunner.js) | Google Apps Script daily digest emailer |

### Outputs
| File | Role |
|------|------|
| [`mws_equity_curve.png`](mws_equity_curve.png) | Equity curve chart (generated on each run) |

---

## Portfolio Architecture

### Buckets
- **Bucket A** (Protected Liquidity): `TREASURY_NOTE` — min $45K market value, never touched
- **Bucket B** (Deployable Capital): All inducted assets + overlays + cash

### Denominator Basis
- **L1/L2 sleeve caps/floors** → Allocatable denominator = TPV − overlays (DBMF, KMLM) − Bucket A
- **Per-ticker caps/floors** → TPV (full portfolio value)
- **Overlay bands** → TPV

### Sleeve Hierarchy

| L1 | Cap | L2 | Floor | Cap | Tickers |
|----|-----|----|-------|-----|---------|
| growth | 60% | ai_tech | 22% | 32% | SOXQ, CHAT, BOTZ, DTCR, GRID |
| growth | 60% | biotech | 4% | 12% | XBI |
| growth | 60% | core_equity | 18% | 38% | VTI, VXUS |
| real_assets | 25% | strategic_materials | 4% | 10% | URNM, REMX, COPX |
| real_assets | 25% | defense_energy | 6% | 14% | XLE, ITA |
| monetary_hedges | 15% | precious_metals | 8% | 15% | IAUM, SIVR |
| speculative | 5% | crypto | 0% | 5% | IBIT |
| stabilizers | 6–12% TPV *(overlay)* | managed_futures | 3% | 6% each | DBMF, KMLM |

### Momentum Engine
- **Blend:** 45% TR12m + 35% 6m slope + 20% 3m residual vs VTI
- **Ranking:** Percentile-ranked within inducted universe, 63-day lookback
- **Floor exit:** 20 consecutive negative days → reduce to zero
- **Re-entry:** 15 consecutive positive days + VIX < 28

### Risk Controls
| Level | Threshold | Action |
|-------|-----------|--------|
| Soft limit | 20% peak-to-trough | Freeze new buys, manual review |
| Hard limit | 28% | Reduce all toward sleeve floors |
| Recovery | <12% for 10 consecutive days | Resume normal rebalancing |

- Turnover cap: 20% per rebalance event, 60% annualized

### Rebalance Triggers
1. **Calendar** — first Monday of month
2. **Band breach** — 3 consecutive days outside band
3. **Signal drift** — >5pp sleeve or >1.5pp ticker drift
4. **Stress override** — soft_limit suspends calendar + signal_drift

---

## Key Design Decisions
- Precious metals (IAUM, SIVR) separated from commodity miners — they behave oppositely in equity drawdowns *(v2.8.0)*
- XLE reclassified from geopolitical_stress overlay → defense_energy sleeve *(v2.8.4)*
- IBIT entered at 167 shares via discretionary override *(2026-03-05, compliance exception)*
- Geopolitical overlay disabled *(2026-03-04)*
- SEPP withdrawal: $45K annually on Jan 5

---

## LLM Access

This repo is public. Raw file URLs for direct LLM ingestion:

```
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_policy.json
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_holdings.csv
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_tracker.json
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_ticker_history.csv
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_recent_performance.csv
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_macro.md
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_titanium_runner.py
```
