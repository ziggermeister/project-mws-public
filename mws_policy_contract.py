import json
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, Any

# -------------------------
# Types / data model
# -------------------------

ALLOWED_STAGES = {"inducted", "activated", "overlay", "reference", "disabled"}

@dataclass(frozen=True)
class SleeveMaps:
    exposure_l2: Dict[str, float]          # must sum to 1.0
    signal_l2: Dict[str, float]            # must sum to 1.0
    l2_to_l1: Dict[str, str]               # each L2 maps to exactly one L1

@dataclass(frozen=True)
class AssetMeta:
    ticker: str
    role: str                              # e.g., "allocatable", "reference", "stabilizer", etc.
    stage: str                             # lifecycle.stage
    entered_stage_date: Optional[str]      # ISO date or None
    benchmark_proxy: Optional[str]         # for alpha proxy (e.g., VTI/QQQ/XLE)
    sleeves: SleeveMaps

@dataclass(frozen=True)
class PolicyContract:
    policy: Dict[str, Any]
    assets: Dict[str, AssetMeta]           # keyed by ticker
    tracker_tickers: List[str]
    drift_policy_not_in_tracker: List[str]
    drift_tracker_not_in_policy: List[str]


# -------------------------
# IO helpers
# -------------------------

def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r") as f:
        return json.load(f)

def read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# -------------------------
# Tracker parsing
# -------------------------

def extract_tracker_universe(tracker: Dict[str, Any]) -> List[str]:
    # Compatible with your historical formats: tickers[], inventory[], positions[]
    out: Set[str] = set()

    def add_t(x: Any) -> None:
        if not x:
            return
        if isinstance(x, str):
            out.add(x.strip().upper())
        elif isinstance(x, dict) and x.get("ticker"):
            out.add(str(x["ticker"]).strip().upper())

    for k in ("tickers", "inventory", "positions"):
        v = tracker.get(k)
        if isinstance(v, list):
            for item in v:
                add_t(item)

    return sorted(out)


# -------------------------
# Policy vNext parsing
# -------------------------

def _sum_close_to_1(weights: Dict[str, float], eps: float = 1e-6) -> bool:
    s = sum(weights.values())
    return abs(s - 1.0) <= eps

def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    # Use only positive weights
    clean = {k: float(v) for k, v in weights.items() if v is not None and float(v) > 0}
    s = sum(clean.values())
    if s <= 0:
        return {}
    return {k: v / s for k, v in clean.items()}

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)

def parse_vnext_assets(policy: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    assets = (policy.get("assets") or {}).get("tickers") or {}
    if not isinstance(assets, dict):
        raise ValueError("policy.assets.tickers must be an object keyed by ticker.")
    # normalize keys to uppercase for internal use
    norm = {}
    for t, meta in assets.items():
        T = str(t).strip().upper()
        norm[T] = meta or {}
    return norm

def parse_l2_to_l1(policy: Dict[str, Any]) -> Dict[str, str]:
    tax = policy.get("taxonomy") or {}
    l2 = (tax.get("sleeves") or {}).get("l2") or {}
    l1 = (tax.get("sleeves") or {}).get("l1") or {}
    # Expect structure: taxonomy.sleeves.l2[<L2>].parent_l1 = <L1>
    out: Dict[str, str] = {}
    if isinstance(l2, dict):
        for l2_name, obj in l2.items():
            if not isinstance(obj, dict):
                continue
            parent = obj.get("parent_l1")
            if parent:
                out[str(l2_name).strip()] = str(parent).strip()
    # Optional: verify parent exists in l1 list/object
    if isinstance(l1, dict):
        for l2_name, parent in out.items():
            _require(parent in l1, f"L2 sleeve '{l2_name}' maps to missing L1 '{parent}'.")
    return out

def build_asset_meta(
    ticker: str,
    raw: Dict[str, Any],
    l2_to_l1: Dict[str, str],
) -> AssetMeta:
    # lifecycle
    lifecycle = raw.get("lifecycle") or {}
    stage = str(lifecycle.get("stage") or "inducted").strip().lower()
    _require(stage in ALLOWED_STAGES, f"{ticker}: invalid lifecycle.stage '{stage}'")

    entered = lifecycle.get("entered_stage_date")
    entered_stage_date = str(entered).strip() if entered else None

    benchmark_proxy = lifecycle.get("benchmark_proxy")
    benchmark_proxy = str(benchmark_proxy).strip().upper() if benchmark_proxy else None

    role = str(raw.get("role") or "").strip().lower() or "allocatable"

    sleeves = raw.get("sleeves") or {}
    exp = sleeves.get("exposure_l2") or {}
    sig = sleeves.get("signal_l2") or None

    # enforce exp exists for inducted/activated/overlay
    if stage in {"inducted", "activated", "overlay"}:
        _require(isinstance(exp, dict) and len(exp) > 0, f"{ticker}: missing sleeves.exposure_l2 for stage={stage}")

    exp_w = _normalize_weights({str(k).strip(): float(v) for k, v in exp.items()}) if isinstance(exp, dict) else {}
    _require(len(exp_w) > 0 or stage not in {"inducted", "activated", "overlay"},
             f"{ticker}: exposure_l2 normalizes to empty")

    # signal defaults to exposure if omitted
    if sig is None:
        sig_w = dict(exp_w)
    else:
        _require(isinstance(sig, dict) and len(sig) > 0, f"{ticker}: sleeves.signal_l2 present but empty")
        sig_w = _normalize_weights({str(k).strip(): float(v) for k, v in sig.items()})

    # must be deterministic sums
    if stage in {"inducted", "activated", "overlay"}:
        _require(_sum_close_to_1(exp_w), f"{ticker}: exposure_l2 weights must sum to 1.0 (after normalization).")
        _require(_sum_close_to_1(sig_w), f"{ticker}: signal_l2 weights must sum to 1.0 (after normalization).")

    # ensure all L2 referenced map to L1
    for l2_name in set(list(exp_w.keys()) + list(sig_w.keys())):
        _require(l2_name in l2_to_l1, f"{ticker}: L2 sleeve '{l2_name}' missing mapping to L1.")

    return AssetMeta(
        ticker=ticker,
        role=role,
        stage=stage,
        entered_stage_date=entered_stage_date,
        benchmark_proxy=benchmark_proxy,
        sleeves=SleeveMaps(exposure_l2=exp_w, signal_l2=sig_w, l2_to_l1=l2_to_l1),
    )


# -------------------------
# Contract build + validation
# -------------------------

def build_policy_contract(
    policy_path: str,
    tracker_path: str,
    strict_tracker_subset: bool = True,      # HARD FAIL: policy ⊆ tracker
    strict_tracker_superset: bool = False,   # optional FAIL: tracker ⊆ policy
) -> PolicyContract:
    policy_bytes = read_file_bytes(policy_path)
    policy = json.loads(policy_bytes.decode("utf-8"))

    tracker = load_json(tracker_path)
    tracker_tickers = extract_tracker_universe(tracker)

    # Optional: validate policy's recorded tracker hash (if present)
    meta = policy.get("meta") or {}
    declared_hash = meta.get("tracker_sha256")
    if declared_hash:
        tracker_hash = _sha256_bytes(read_file_bytes(tracker_path))
        _require(
            str(declared_hash).strip().lower() == tracker_hash.lower(),
            f"Tracker hash mismatch. Policy expects {declared_hash}, actual {tracker_hash}."
        )

    assets_raw = parse_vnext_assets(policy)
    policy_tickers = sorted(assets_raw.keys())

    policy_set = set(policy_tickers)
    tracker_set = set(tracker_tickers)

    drift_policy_not_in_tracker = sorted(list(policy_set - tracker_set))
    drift_tracker_not_in_policy = sorted(list(tracker_set - policy_set))

    if strict_tracker_subset:
        _require(
            len(drift_policy_not_in_tracker) == 0,
            f"HARD_FAIL: policy has tickers not in tracker: {drift_policy_not_in_tracker}"
        )

    if strict_tracker_superset:
        _require(
            len(drift_tracker_not_in_policy) == 0,
            f"HARD_FAIL: tracker has tickers not in policy: {drift_tracker_not_in_policy}"
        )

    # Sleeve taxonomy
    l2_to_l1 = parse_l2_to_l1(policy)
    _require(len(l2_to_l1) > 0, "taxonomy.sleeves.l2 mapping is missing/empty.")

    # Build AssetMeta objects
    assets: Dict[str, AssetMeta] = {}
    for t, raw in assets_raw.items():
        assets[t] = build_asset_meta(t, raw, l2_to_l1)

    # Additional global validations:
    # 1) Ensure every L2 maps to exactly one L1 (already enforced by dict)
    # 2) Ensure lifecycle stages are lowercase and allowed (done)
    # 3) Ensure benchmark proxies are in tracker universe (optional but recommended)
    for t, a in assets.items():
        if a.benchmark_proxy:
            _require(a.benchmark_proxy in tracker_set,
                     f"{t}: benchmark_proxy {a.benchmark_proxy} not in tracker universe.")

    return PolicyContract(
        policy=policy,
        assets=assets,
        tracker_tickers=tracker_tickers,
        drift_policy_not_in_tracker=drift_policy_not_in_tracker,
        drift_tracker_not_in_policy=drift_tracker_not_in_policy,
    )


# -------------------------
# Deterministic projections used by runners
# -------------------------

def project_universe(contract: PolicyContract) -> List[str]:
    # Operational fetch universe (tracker): this is what you backfill/download
    return list(contract.tracker_tickers)

def project_eligibility(contract: PolicyContract) -> Dict[str, Set[str]]:
    # Deterministic policy → eligibility sets (allocator-ready)
    inducted = {t for t, a in contract.assets.items() if a.stage == "inducted"}
    activated = {t for t, a in contract.assets.items() if a.stage == "activated"}
    overlay = {t for t, a in contract.assets.items() if a.stage == "overlay"}
    reference = {t for t, a in contract.assets.items() if a.stage == "reference"}
    disabled = {t for t, a in contract.assets.items() if a.stage == "disabled"}

    # Candidates for ranking/signals:
    candidates = set(inducted) | set(activated) | set(overlay)
    # (You can later exclude overlays from optimization but still score them)

    return {
        "inducted": inducted,
        "activated": activated,
        "overlay": overlay,
        "reference": reference,
        "disabled": disabled,
        "candidates": candidates,
    }

def project_sleeve_rollups(contract: PolicyContract) -> Dict[str, Any]:
    # Returns the deterministic mappings a future script will use:
    # ticker -> L2 exposure weights, ticker -> L2 signal weights, L2 -> L1 parent
    return {
        "ticker_exposure_l2": {t: a.sleeves.exposure_l2 for t, a in contract.assets.items()},
        "ticker_signal_l2": {t: a.sleeves.signal_l2 for t, a in contract.assets.items()},
        "l2_to_l1": dict(next(iter(contract.assets.values())).sleeves.l2_to_l1) if contract.assets else {},
    }
