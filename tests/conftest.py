"""
tests/conftest.py — Shared fixtures for the MWS Phase 1 test suite.

All fixtures are designed to be self-contained and file-I/O isolated:
every test that writes to disk uses tmp_path and monkeypatches the module
constants so production files are never touched.
"""
import csv
import io
import json
import os
import sys
import types

import numpy as np
import pandas as pd
import pytest


# ── Make sure the repo root is on sys.path so mws_analytics and mws_runner import ──
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Patch matplotlib to Agg backend BEFORE any import that touches pyplot
import matplotlib
matplotlib.use("Agg")

import mws_analytics  # noqa: E402


# ── Synthetic price history ────────────────────────────────────────────────────

def make_hist(tickers=None, n_rows=300, seed=42):
    """
    Return a wide-format pd.DataFrame with a DatetimeIndex and one column per
    ticker.  Each column is a synthetic random-walk price series starting at
    100.0 with small positive drift, suitable for all analytics functions.

    Parameters
    ----------
    tickers : list[str], optional
        Defaults to ['VTI', 'IAUM', 'URNM', 'DBMF', 'IBIT', 'VXUS', 'XBI'].
    n_rows : int
        Number of trading-day rows (default 300).
    seed : int
        NumPy random seed for reproducibility (default 42).
    """
    if tickers is None:
        tickers = ["VTI", "IAUM", "URNM", "DBMF", "IBIT", "VXUS", "XBI"]

    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-03-20", periods=n_rows)
    data = {}
    for t in tickers:
        # Small positive drift + random noise
        log_rets = rng.normal(loc=0.0003, scale=0.012, size=n_rows)
        prices = 100.0 * np.exp(np.cumsum(log_rets))
        prices[0] = 100.0
        data[t] = prices

    df = pd.DataFrame(data, index=idx)
    df.index.name = "Date"
    return df


def make_holdings(tickers_mv: dict):
    """
    Build a holdings DataFrame matching the format _build_portfolio_tables() expects.

    Parameters
    ----------
    tickers_mv : dict
        Mapping ticker → (shares, price, class_name).
        Example: {"VTI": (100, 230.0, "core_equity")}

    Returns a pd.DataFrame with columns: Ticker, Shares, Price, MV, Class.
    """
    rows = []
    for ticker, spec in tickers_mv.items():
        shares, price, cls = spec
        rows.append({
            "Ticker": ticker,
            "Shares": shares,
            "Price":  price,
            "MV":     shares * price,
            "Class":  cls,
        })
    return pd.DataFrame(rows)


def make_scores(tickers_pct: dict, tickers_raw: dict = None):
    """
    Build a df_scores DataFrame matching generate_rankings() output format.

    Parameters
    ----------
    tickers_pct : dict
        Mapping ticker → Pct (0.0–1.0).
    tickers_raw : dict, optional
        Mapping ticker → RawScore (float). Defaults to Pct - 0.5 (positive when Pct > 0.5).
    """
    if tickers_raw is None:
        tickers_raw = {t: pct - 0.5 for t, pct in tickers_pct.items()}

    rows = []
    pcts = sorted(tickers_pct.values(), reverse=True)
    for ticker, pct in tickers_pct.items():
        rank = pcts.index(pct) + 1
        rows.append({
            "Ticker":   ticker,
            "Score":    pct,
            "Pct":      pct,
            "RawScore": tickers_raw.get(ticker, pct - 0.5),
            "Alpha":    "N/A",
            "AlphaVs":  "VTI",
            "Sleeve":   "UNMAPPED",
            "Status":   "INDUCTED/HELD",
        })
    return pd.DataFrame(rows)


def make_gate_rows(tickers, gate_action_buy="proceed", gate_action_sell="proceed"):
    """
    Build a df_gates DataFrame matching the format produced by run_analytics().

    Parameters
    ----------
    tickers : list[str]
    gate_action_buy : str
        "proceed" | "defer" — applied to all tickers unless tickers is a dict.
    gate_action_sell : str
        "proceed" | "defer" | "spike_trim" — applied to all tickers.
    """
    rows = []
    for t in tickers:
        if isinstance(gate_action_buy, dict):
            buy_action  = gate_action_buy.get(t, "proceed")
        else:
            buy_action  = gate_action_buy
        if isinstance(gate_action_sell, dict):
            sell_action = gate_action_sell.get(t, "proceed")
        else:
            sell_action = gate_action_sell
        rows.append({
            "ticker":           t,
            "gate_action":      buy_action,  # legacy field
            "gate_action_buy":  buy_action,
            "gate_action_sell": sell_action,
            "z_score":          0.0,
            "vol_clamp":        "none",
            "raw_vol_pct":      0.5,
            "eff_vol_pct":      0.5,
        })
    return pd.DataFrame(rows)


# ── Canonical minimal policy ───────────────────────────────────────────────────

def make_policy(**overrides):
    """
    Return a complete minimal policy dict suitable for _build_portfolio_tables().

    Uses CORRECT key names (learned from bugs #4 and #15):
      - drawdown_rules   (NOT risk_controls)
      - definitions.buckets.bucket_a_protected_liquidity.minimum_usd (NOT bucket_a.minimum_balance)

    Override any top-level key via keyword arguments.
    """
    base = {
        "meta": {
            "policy_version": "v_test",
            "last_updated":   "2026-03-20",
        },
        "governance": {
            "reporting_baselines": {
                "active_benchmarks":  ["SPY"],
                "corr_anchor_ticker": "VTI",
                "chart_start_date":   "2026-01-01",
                "alpha_start_date":   "2026-01-01",
            },
            "fixed_asset_prices": {
                "CASH":          1.0,
                "TREASURY_NOTE": {
                    "price_type":    "fixed",
                    "fallback_price": 45000,
                },
            },
            "execution": {
                "max_turnover": 0.20,
            },
        },
        # Correct key — bug #4 was using "risk_controls" instead
        "drawdown_rules": {
            "enabled":    True,
            "soft_limit": 0.22,
            "hard_limit": 0.30,
            "recovery_condition": {"drawdown_below": 0.15},
        },
        # Correct path — bug #15 was reading from wrong field
        "definitions": {
            "buckets": {
                "bucket_a_protected_liquidity": {
                    "minimum_usd": 45000,
                }
            }
        },
        "sleeves": {
            "level1": {
                "growth": {
                    "cap":      0.60,
                    "children": ["core_equity", "ai_tech", "biotech"],
                },
                "real_assets": {
                    "cap":      0.25,
                    "children": ["strategic_materials", "precious_metals"],
                },
                "monetary_hedges": {
                    "cap":      0.15,
                    "children": ["precious_metals"],
                },
                "speculative": {
                    "cap":      0.05,
                    "children": ["crypto"],
                },
                "stabilizers": {
                    # No cap — overlay sleeve
                    "children": ["managed_futures"],
                },
            },
            "level2": {
                "core_equity": {
                    "l1_parent": "growth",
                    "floor": 0.18,
                    "cap":   0.38,
                    "tickers": ["VTI", "VXUS"],
                },
                "ai_tech": {
                    "l1_parent": "growth",
                    "floor": {
                        "type": "breadth_conditioned",
                        "breadth_condition": {
                            "strong_breadth_threshold": 3,
                            "hysteresis_days": 5,
                        },
                        "strong_breadth_floor": 0.22,
                        "weak_breadth_floor":   0.12,
                        "infeasible_floor":     0.0,
                        "infeasible_condition": "positive_count == 0",
                    },
                    "cap":   0.32,
                    "tickers": ["SOXQ", "CHAT", "BOTZ", "DTCR", "GRID"],
                },
                "biotech": {
                    "l1_parent": "growth",
                    "floor": 0.04,
                    "cap":   0.12,
                    "tickers": ["XBI"],
                },
                "strategic_materials": {
                    "l1_parent": "real_assets",
                    "floor": 0.04,
                    "cap":   0.10,
                    "tickers": ["URNM", "REMX", "COPX"],
                },
                "precious_metals": {
                    "l1_parent": "monetary_hedges",
                    "floor": 0.08,
                    "cap":   0.15,
                    "tickers": ["IAUM", "SIVR"],
                },
                "crypto": {
                    "l1_parent": "speculative",
                    "floor": 0.0,
                    "cap":   0.05,
                    "tickers": ["IBIT"],
                },
                "managed_futures": {
                    "l1_parent":              "stabilizers",
                    "floor":                  0.06,
                    "cap":                    0.12,
                    "exclude_from_denominator": True,
                    "tickers": ["DBMF", "KMLM"],
                },
            },
        },
        "ticker_constraints": {
            "VTI":  {"min_total": 0.10, "max_total": 0.25, "lifecycle": {"stage": "inducted"}},
            "VXUS": {"min_total": 0.04, "max_total": 0.12, "lifecycle": {"stage": "inducted"}},
            "IAUM": {"max_total": 0.08,                    "lifecycle": {"stage": "inducted"}},
            "SIVR": {"min_total": 0.03, "max_total": 0.06, "lifecycle": {"stage": "inducted"}},
            "URNM": {"max_total": 0.04,                    "lifecycle": {"stage": "inducted"}},
            "REMX": {"max_total": 0.04,                    "lifecycle": {"stage": "inducted"}},
            "COPX": {"max_total": 0.04,                    "lifecycle": {"stage": "inducted"}},
            "IBIT": {"max_total": 0.05,                    "lifecycle": {"stage": "inducted"}},
            "XBI":  {"max_total": 0.06,                    "lifecycle": {"stage": "inducted"}},
            "DBMF": {"min_total": 0.03, "max_total": 0.06, "lifecycle": {"stage": "overlay"},
                     "is_overlay": True, "exclude_from_denominator": True,
                     "eligible_for_allocation": False, "eligible_for_momentum": False},
            "KMLM": {"min_total": 0.03, "max_total": 0.06, "lifecycle": {"stage": "overlay"},
                     "is_overlay": True, "exclude_from_denominator": True,
                     "eligible_for_allocation": False, "eligible_for_momentum": False},
            "SOXQ": {"max_total": 0.10, "lifecycle": {"stage": "inducted"}},
            "CHAT": {"max_total": 0.08, "lifecycle": {"stage": "inducted"}},
            "BOTZ": {"max_total": 0.02, "lifecycle": {"stage": "inducted"}},
            "DTCR": {"max_total": 0.06, "lifecycle": {"stage": "inducted"}},
            "GRID": {"max_total": 0.05, "lifecycle": {"stage": "inducted"}},
            "ITA":  {"max_total": 0.10, "lifecycle": {"stage": "inducted"}},
            "XLE":  {"max_total": 0.08, "lifecycle": {"stage": "inducted"}},
        },
        "ticker_to_sleeves": {
            "VTI":  {"core_equity": 1.0},
            "VXUS": {"core_equity": 1.0},
            "IAUM": {"precious_metals": 1.0},
            "SIVR": {"precious_metals": 1.0},
            "URNM": {"strategic_materials": 1.0},
            "REMX": {"strategic_materials": 1.0},
            "COPX": {"strategic_materials": 1.0},
            "IBIT": {"crypto": 1.0},
            "XBI":  {"biotech": 1.0},
            "DBMF": {"managed_futures": 1.0},
            "KMLM": {"managed_futures": 1.0},
            "SOXQ": {"ai_tech": 1.0},
            "CHAT": {"ai_tech": 1.0},
            "BOTZ": {"ai_tech": 1.0},
            "DTCR": {"ai_tech": 1.0},
            "GRID": {"ai_tech": 1.0},
            "ITA":  {"defense_energy": 1.0},
            "XLE":  {"defense_energy": 1.0},
        },
        "execution_gates": {
            "short_term_confirmation": {
                "enabled":                  False,
                "buy_defer_sigma":          2.0,
                "sell_defer_sigma":         2.5,
                "max_defer_calendar_days":  10,
                "ewma_span_days":           126,
                "vol_clamp_enabled":        False,
            },
            "per_ticker_thresholds": {},
        },
        "tactical_cash_management": {
            "enabled":                 False,
            "cash_reserve_buffer_pct": 0.01,
            "persistence_required_days": 2,
            "tactical_cash_cap_pct":   0.30,
        },
        "cash_reserve": {
            "enabled": False,
        },
        "momentum_engine": {
            "signal_weights": {
                "tr_12m":      0.45,
                "slope_6m":    0.35,
                "residual_3m": 0.20,
            },
        },
    }

    # Apply overrides (shallow merge at top level)
    for k, v in overrides.items():
        base[k] = v
    return base


# ── Runner invocation helper ───────────────────────────────────────────────────

def run_portfolio_tables(policy, holdings, hist, scores, gates, tmp_path, monkeypatch):
    """
    Call _build_portfolio_tables() in a fully isolated environment and return
    the parsed mws_precomputed_targets.json dict.

    Patches:
      - mws_analytics.BREADTH_STATE_JSON        → tmp_path/breadth_state.json
      - mws_analytics.TACTICAL_CASH_STATE_JSON  → tmp_path/tactical_cash_state.json
      - mws_runner.PRECOMPUTED_TARGETS_FILE      → tmp_path/precomputed_targets.json
      - mws_runner.HOLDINGS_FILE                → tmp_path/holdings.csv (for hash)

    The holdings DataFrame must already have Price and MV columns
    (use make_holdings()).

    Returns the parsed precomputed_targets JSON dict, or raises if the file
    was not written (which indicates _build_portfolio_tables() threw an
    unhandled exception).
    """
    import mws_runner  # noqa: delay import to avoid circular issues

    breadth_path   = str(tmp_path / "breadth_state.json")
    tactical_path  = str(tmp_path / "tactical_cash_state.json")
    targets_path   = str(tmp_path / "precomputed_targets.json")
    holdings_csv   = str(tmp_path / "holdings.csv")

    # Write holdings CSV for hash computation inside _build_portfolio_tables
    holdings.to_csv(holdings_csv, index=False)

    monkeypatch.setattr(mws_analytics, "BREADTH_STATE_JSON",       breadth_path)
    monkeypatch.setattr(mws_analytics, "TACTICAL_CASH_STATE_JSON",  tactical_path)
    monkeypatch.setattr(mws_runner,    "PRECOMPUTED_TARGETS_FILE",   targets_path)
    monkeypatch.setattr(mws_analytics, "HOLDINGS_CSV",              holdings_csv)

    # Compute total_val from holdings
    total_val = float(holdings["MV"].sum())
    val_asof  = str(hist.index.max().date())

    _dr = policy.get("drawdown_rules", {})
    analytics = {
        "policy":       policy,
        "holdings":     holdings,
        "hist":         hist,
        "total_val":    total_val,
        "val_asof":     val_asof,
        "drawdown":     {
            "state":      "normal",
            "drawdown":   0.0,
            "soft_limit": _dr.get("soft_limit", 0.22),
            "hard_limit": _dr.get("hard_limit", 0.30),
        },
        "df_scores":    scores,
        "df_gates":     gates,
    }

    # Call the function — it returns HTML but also writes the JSON as a side effect
    mws_runner._build_portfolio_tables(analytics)

    if not os.path.exists(targets_path):
        raise RuntimeError(
            "_build_portfolio_tables() did not write precomputed_targets.json. "
            "Check runner logs above for the exception."
        )

    with open(targets_path, encoding="utf-8") as f:
        return json.load(f)
