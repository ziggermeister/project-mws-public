#!/usr/bin/env python3
"""
trigger_run.py — Trigger the MWS Portfolio Run GitHub Actions workflow on demand.

Usage:
    python3 trigger_run.py                      # trigger and tail logs
    python3 trigger_run.py --no-tail            # trigger only, don't wait for logs
    python3 trigger_run.py --no-llm             # analytics + chart only, skip LLM + email
    python3 trigger_run.py --local              # run locally instead (requires env vars)
    python3 trigger_run.py --local --no-llm     # local analytics only, no LLM
    python3 trigger_run.py --local --no-tail    # local run, exit immediately

GitHub Actions triggers the same workflow as:
    GitHub → Actions → "MWS Portfolio Run" → Run workflow

Requirements (for --remote, the default):
    gh CLI installed and authenticated:  brew install gh && gh auth login

Requirements (for --local):
    ANTHROPIC_API_KEY, GMAIL_APP_PASSWORD, GMAIL_FROM, GMAIL_TO env vars set.
    (Not needed with --no-llm)
"""
import argparse
import os
import subprocess
import sys
import time

WORKFLOW_NAME = "MWS Portfolio Run"

# ── helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    kwargs: dict = {"check": check}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(cmd, **kwargs)


def gh_available() -> bool:
    try:
        run(["gh", "--version"], capture=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def trigger_github_actions(no_llm: bool = False) -> None:
    label = " (analytics only — LLM skipped)" if no_llm else ""
    print(f"Triggering GitHub Actions: '{WORKFLOW_NAME}'{label} ...")
    cmd = ["gh", "workflow", "run", WORKFLOW_NAME]
    if no_llm:
        cmd += ["--field", "skip_llm=true"]
    try:
        run(cmd)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to trigger workflow: {e}", file=sys.stderr)
        print("Make sure `gh` is installed (brew install gh) and authenticated (gh auth login).",
              file=sys.stderr)
        sys.exit(1)
    print("Workflow dispatched.")


def tail_github_run(wait_seconds: int = 15) -> None:
    """Wait for the run to appear, then tail its logs."""
    print(f"Waiting {wait_seconds}s for run to appear ...")
    time.sleep(wait_seconds)

    result = run(
        ["gh", "run", "list", "--workflow", WORKFLOW_NAME,
         "--limit", "1", "--json", "databaseId,status,conclusion"],
        capture=True,
    )
    import json
    runs = json.loads(result.stdout)
    if not runs:
        print("No run found yet — check GitHub Actions manually.", file=sys.stderr)
        return

    run_id = str(runs[0]["databaseId"])
    print(f"Run ID: {run_id}  — tailing logs (Ctrl-C to detach) ...")
    print("-" * 60)

    try:
        run(["gh", "run", "watch", run_id], check=False)
    except KeyboardInterrupt:
        print("\nDetached from log stream. Run continues in GitHub Actions.")
        print(f"View at: gh run view {run_id} --log")


def run_local(no_llm: bool) -> None:
    """Run mws_runner.py locally in the same directory as this script."""
    here = os.path.dirname(os.path.abspath(__file__))
    runner = os.path.join(here, "mws_runner.py")
    if not os.path.exists(runner):
        print(f"ERROR: mws_runner.py not found at {runner}", file=sys.stderr)
        sys.exit(1)

    if no_llm:
        print("Running mws_runner.py locally (analytics + chart only — LLM skipped) ...")
        env = {**os.environ, "SKIP_LLM": "true"}
        result = subprocess.run([sys.executable, runner], cwd=here, env=env, check=False)
        sys.exit(result.returncode)

    missing = [v for v in ("ANTHROPIC_API_KEY", "GMAIL_APP_PASSWORD", "GMAIL_FROM", "GMAIL_TO")
               if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}", file=sys.stderr)
        print("Export them before running (or use --no-llm to skip the LLM call):",
              file=sys.stderr)
        for v in missing:
            print(f"  export {v}=...", file=sys.stderr)
        sys.exit(1)

    print("Running mws_runner.py locally ...")
    result = subprocess.run([sys.executable, runner], cwd=here, check=False)
    sys.exit(result.returncode)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local",  action="store_true",
                        help="Run locally instead of triggering GitHub Actions")
    parser.add_argument("--no-tail", action="store_true",
                        help="Don't wait for / tail the run logs after triggering")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM call and email — analytics + chart only (saves tokens)")
    args = parser.parse_args()

    if args.local:
        run_local(no_llm=args.no_llm)
        return

    if not gh_available():
        print("ERROR: `gh` CLI not found. Install with: brew install gh", file=sys.stderr)
        print("Then authenticate: gh auth login", file=sys.stderr)
        sys.exit(1)

    trigger_github_actions(no_llm=args.no_llm)

    if not args.no_tail:
        tail_github_run()
    else:
        print("Done. Check progress at: gh run list --workflow 'MWS Portfolio Run'")


if __name__ == "__main__":
    main()
