#!/usr/bin/env python3
"""Dispatch a workflow_dispatch-triggered workflow in another repo via the
raw REST API.

Used by e2e-apps' callback mode (wait-mode: callback) — the dispatch half of
what codex-/return-dispatch does internally, minus the run-locate polling
that mode doesn't need (the connector reports back directly; see
complete_check_run.py). `gh workflow run` only accepts inputs as individual
`-f key=value` flags, which doesn't fit an arbitrary JSON object of inputs
assembled by the caller (pull_request.yaml's workflow-inputs), so this goes
straight to the dispatches endpoint with a JSON body instead.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the `gh` CLI."""
    return subprocess.run(cmd, **kwargs)


def dispatch(repo: str, workflow: str, ref: str, inputs: dict) -> None:
    payload = {"ref": ref, "inputs": inputs}
    result = run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repo}/actions/workflows/{workflow}/dispatches",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::failed to dispatch {workflow} on {repo}@{ref}: {result.stderr}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. atlanhq/atlan-mysql-app"
    )
    parser.add_argument(
        "--workflow", required=True, help="Workflow filename, e.g. tests.yaml"
    )
    parser.add_argument("--ref", required=True)
    parser.add_argument(
        "--inputs", required=True, help="JSON object of workflow_dispatch inputs"
    )
    args = parser.parse_args(argv)

    try:
        inputs = json.loads(args.inputs)
    except json.JSONDecodeError as e:
        raise SystemExit(f"::error::--inputs is not valid JSON: {e}")
    if not isinstance(inputs, dict):
        raise SystemExit("::error::--inputs must be a JSON object")

    dispatch(args.repo, args.workflow, args.ref, inputs)
    print(f"Dispatched {args.workflow} on {args.repo}@{args.ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
