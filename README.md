# Momentum-Weighted Scaling (MWS) v2.9.2

A systematic, rules-based portfolio management system for a personal investment account with annual SEPP withdrawals. Combines momentum signals with strict sleeve caps/floors and an execution gate timing filter to outperform VTI by ≥150bps net of turnover costs over a 10-year horizon.

---

## ⚠️ Authority & Precedence

> **`mws_policy.json` is the single authoritative source of truth.**
> `mws_macro.md` is advisory/explanatory only.
> Execution code is authoritative over prose.
> Any conflict: policy JSON wins.

---

## Run Modes

| Mode | Command | What it does |
|------|---------|--------------|
| **GitHub Actions — LLM run** | Auto: 14:30 UTC + 22:00 UTC weekdays | Calls Claude API → news fetch → momentum → rebalance reco → schema validation → HTML email → commits outputs |
| **GitHub Actions — price fetch** | Auto: 21:30 UTC weekdays | Fetches latest prices, commits `mws_ticker_history.csv` |
| **On-demand LLM run** | GitHub → Actions → "MWS Portfolio Run" → Run workflow | Same as scheduled LLM run |
| **Local LLM run** | `python mws_runner.py` | Full LLM run locally (requires env vars: `ANTHROPIC_API_KEY`, `GMAIL_APP_PASSWORD`, `GMAIL_FROM`, `GMAIL_TO`) |
| **Local data sync** | `./commit_and_run.sh` | Commits changed data files, pushes to main, runs `mws_analytics.py` for local charts + diagnostics. No LLM call, no email. |

> **Scheduling:** Price fetch runs at 21:30 UTC; evening LLM run is offset to 22:00 UTC to ensure fresh prices are committed before the LLM run checks out the repo.

---

## File Index

### Policy
| File | Role |
|------|------|
| [`mws_policy.json`](mws_policy.json) | **Binding rule set** — caps, floors, signals, constraints, lifecycle rules, execution gate |
| [`mws_macro.md`](mws_macro.md) | Explanatory governance doc (advisory only) |

### Data
| File | Role |
|------|------|
| [`mws_holdings.csv`](mws_holdings.csv) | Current holdings (Ticker, Shares, Class) |
| [`mws_tracker.json`](mws_tracker.json) | State tracker — inducted universe, signals, peak TPV, deferred trades |
| [`mws_ticker_history.csv`](mws_ticker_history.csv) | Historical price data (Date, Ticker, AdjClose) |
| [`mws_recent_performance.csv`](mws_recent_performance.csv) | Daily performance log with TWR and benchmark comparison |
| [`mws_run_results.csv`](mws_run_results.csv) | Run output log |
| [`mws_market_context.md`](mws_market_context.md) | Latest LLM market context output (written each run) |

### Execution
| File | Role |
|------|------|
| [`mws_runner.py`](mws_runner.py) | Main runner — price fetch, LLM call, schema validation, email |
| [`mws_analytics.py`](mws_analytics.py) | Local analytics engine — charts, breach flags, diagnostics |
| [`mws_fetch_history.py`](mws_fetch_history.py) | Incremental price history fetcher (used by daily GitHub Action) |
| [`mws_llm_run_prompt.md`](mws_llm_run_prompt.md) | Canonical LLM run prompt (injected at runtime; also usable manually with ChatGPT/Gemini) |
| [`commit_and_run.sh`](commit_and_run.sh) | Local workflow: commit data files, push, run local analytics |
| [`convert.py`](convert.py) | One-off utility: converts Chase positions.csv export → `mws_holdings.csv` |

### CI / Automation
| File | Role |
|------|------|
| [`.github/workflows/mws_run.yml`](.github/workflows/mws_run.yml) | LLM run — weekdays 14:30 UTC + 21:30 UTC |
| [`.github/workflows/mws_daily.yml`](.github/workflows/mws_daily.yml) | Price fetch — weekdays 21:30 UTC |
| [`requirements.txt`](requirements.txt) | Python dependencies |
| [`test_mws.py`](test_mws.py) | Test suite |

### Outputs
| File | Role |
|------|------|
| `mws_equity_curve.png` | Equity curve chart (regenerated on each local run, gitignored) |

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

### Execution Gate (v2.9.0+)
Timing filter applied to momentum-driven trades only. Does not change target weights.

| Condition | Action |
|-----------|--------|
| Buy z-score ≥ +2.0σ | Defer up to 10 calendar days (don't chase spikes) |
| Sell z-score ≤ −2.5σ | Defer up to 10 calendar days (don't sell into capitulation) |
| Sell z-score ≥ +2.0σ | Execute immediately — spike-trim (sell into strength) |
| During soft_limit | Sell-defer collapses to 3 calendar days max |

- Method: Per-ticker EWMA vol-scaled z-score (126-day span, 2-day lookback)
- Spike-trim rebuy rate monitored: >5% over 90 days activates `spike_trim_reentry_buffer` (0.75% weight gap)
- Reviewed by ChatGPT + Gemini over 4 rounds; declared production-ready 2026-03-10

### Schema Validation
Each LLM run output is validated before email is sent:
- Response must contain exactly one `<mws_market_context>` block and one `<mws_recommendation>` block
- No text outside those two blocks
- Each block ≤ 60,000 chars; total response ≤ 120,000 chars
- Violation → `SchemaViolationError` → alert email only → `sys.exit(1)` → GitHub Actions marks run FAILED

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
4. **Stress override** — soft_limit suspends calendar + signal_drift triggers

---

## Key Design Decisions
- Precious metals (IAUM, SIVR) separated from commodity miners — they behave oppositely in equity drawdowns *(v2.8.0)*
- XLE reclassified from geopolitical_stress overlay → defense_energy sleeve *(v2.8.4)*
- IBIT entered at 167 shares via discretionary override *(2026-03-05, compliance exception)*
- Geopolitical overlay disabled *(2026-03-04)*
- Execution gate added *(v2.9.0)*, finalized after 4-round LLM review *(v2.9.2, 2026-03-10)*
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
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_analytics.py
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_runner.py
https://raw.githubusercontent.com/ziggermeister/project-mws-public/main/mws_llm_run_prompt.md
```
