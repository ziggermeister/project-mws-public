"""
tests/policy/test_json_lint.py

JSON lint tests: every repo-owned .json file must be parseable as valid UTF-8 JSON
with the expected top-level type.

Scope:
  - All .json files under the repo root (2 levels deep, non-recursive through worktrees)
  - Excludes .claude/ worktrees (they are isolated copies, not repo sources)
  - Excludes node_modules/ and __pycache__/
  - Golden files in tests/golden/ are included (they must remain valid JSON)

Key artifact checks (beyond "is it valid JSON"):
  - mws_policy.json            → top-level dict
  - mws_policy_runtime.json    → top-level dict (if present)
  - mws_precomputed_targets.json → top-level dict (if present)
  - mws_breadth_state.json     → top-level dict (if present)
  - mws_tactical_cash_state.json → top-level dict (if present)
  - tests/golden/scenario_*.json → top-level dict (each ticker entry is a dict)

The "no-legacy-key" test ensures no fixture or runtime JSON contains the obsolete
'risk_controls' key that caused Bug #4.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Repo root is two levels above this file (tests/policy/ → tests/ → repo root)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _collect_repo_json_files():
    """
    Walk the repo root and collect all .json files that are part of the
    source tree.  Excludes worktrees (.claude/), node_modules, __pycache__,
    and hidden directories other than .claude (which we skip entirely).
    """
    excluded_dirs = {".claude", "node_modules", "__pycache__", ".git"}
    json_files = []
    for root, dirs, files in os.walk(_REPO_ROOT):
        # Prune excluded directories in-place so os.walk won't recurse into them
        dirs[:] = [d for d in dirs if d not in excluded_dirs]
        for fname in files:
            if fname.endswith(".json"):
                json_files.append(os.path.join(root, fname))
    return sorted(json_files)


_ALL_JSON_FILES = _collect_repo_json_files()


# ── Parametrised lint test ────────────────────────────────────────────────────

@pytest.mark.parametrize("json_path", _ALL_JSON_FILES, ids=lambda p: os.path.relpath(p, _REPO_ROOT))
def test_json_file_is_valid(json_path):
    """Every repo JSON file must parse as valid UTF-8 JSON."""
    rel = os.path.relpath(json_path, _REPO_ROOT)
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        pytest.fail(f"{rel}: JSON parse error — {exc}")
    except UnicodeDecodeError as exc:
        pytest.fail(f"{rel}: UTF-8 decode error — {exc}")
    # Parsed successfully; data is available for further checks below.
    _ = data  # silence linter


# ── Key-artifact structural checks ───────────────────────────────────────────

class TestKeyArtifactStructure:
    """Spot-check that critical JSON files have the expected top-level shape."""

    def _load(self, filename):
        path = os.path.join(_REPO_ROOT, filename)
        if not os.path.exists(path):
            pytest.skip(f"{filename} not found (auto-generated, may not exist in CI)")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_policy_is_dict(self):
        data = self._load("mws_policy.json")
        assert isinstance(data, dict), "mws_policy.json must be a JSON object (dict)"

    def test_policy_runtime_is_dict(self):
        data = self._load("mws_policy_runtime.json")
        assert isinstance(data, dict), "mws_policy_runtime.json must be a JSON object (dict)"

    def test_precomputed_targets_is_dict(self):
        data = self._load("mws_precomputed_targets.json")
        assert isinstance(data, dict), "mws_precomputed_targets.json must be a JSON object (dict)"

    def test_breadth_state_is_dict(self):
        data = self._load("mws_breadth_state.json")
        assert isinstance(data, dict), "mws_breadth_state.json must be a JSON object (dict)"

    def test_tactical_cash_state_is_dict(self):
        data = self._load("mws_tactical_cash_state.json")
        assert isinstance(data, dict), "mws_tactical_cash_state.json must be a JSON object (dict)"

    def test_golden_scenario_files_are_dicts(self):
        """Each golden scenario file must be a JSON object (ticker → row dict)."""
        golden_dir = os.path.join(_REPO_ROOT, "tests", "golden")
        if not os.path.isdir(golden_dir):
            pytest.skip("tests/golden/ directory not found")
        failures = []
        for fname in sorted(os.listdir(golden_dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(golden_dir, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                failures.append(f"{fname}: {exc}")
                continue
            if not isinstance(data, dict):
                failures.append(f"{fname}: expected dict, got {type(data).__name__}")
        assert not failures, "Golden file structure failures:\n" + "\n".join(failures)


# ── No-legacy-key test ────────────────────────────────────────────────────────

class TestNoLegacyPolicyKey:
    """
    Bug #4 regression: 'risk_controls' is the obsolete key — the correct key
    is 'drawdown_rules'.  No repo-owned JSON file should contain 'risk_controls'
    as a top-level key, since that indicates either a stale file or a fixture
    that would cause check_drawdown_state() to silently use wrong thresholds.
    """

    @pytest.mark.parametrize(
        "json_path",
        _ALL_JSON_FILES,
        ids=lambda p: os.path.relpath(p, _REPO_ROOT),
    )
    def test_no_risk_controls_key(self, json_path):
        rel = os.path.relpath(json_path, _REPO_ROOT)
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pytest.skip(f"{rel}: could not parse (caught by lint test)")
            return
        if isinstance(data, dict) and "risk_controls" in data:
            pytest.fail(
                f"{rel}: contains obsolete top-level key 'risk_controls'. "
                "Bug #4: the correct key is 'drawdown_rules'. "
                "Update this file or remove the legacy key."
            )
