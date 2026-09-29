#!/usr/bin/env python3
"""Dispatch the vuln triage once per newly-filed security ticket.

``security_scan_create_linear.py`` emits a ``new_issues`` output — a JSON
array of ``{identifier, url, severity}``. This script reads that array and
runs ``gh workflow run vuln-triage.yml`` once per ticket, passing the ticket
identifier, its severity and the scan run that filed it (so the triage reads
exactly those artifacts, not a later hourly scan).

Loop logic lives here (a tested script) rather than inlined in the workflow
YAML, per docs/standards/ci.md.

Environment:
    NEW_ISSUES   JSON array of {identifier, severity, ...} (default "[]")
    GH_REF       git ref to dispatch the workflow on (default "main")
    SCAN_RUN_ID  the dispatching scan's run id (optional)
    GH_TOKEN     consumed by ``gh`` for auth (not read here directly)

Optional:
    VULN_TRIAGE_WORKFLOW   workflow file name (default "vuln-triage.yml")
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

DEFAULT_WORKFLOW = "vuln-triage.yml"


def parse_issues(raw: str | None) -> list[dict[str, Any]]:
    """Parse the NEW_ISSUES JSON, tolerating empty/missing input."""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"::error::NEW_ISSUES is not valid JSON: {e}")
    if not isinstance(data, list):
        raise SystemExit("::error::NEW_ISSUES must be a JSON array")
    return data


def dispatch(
    issues: list[dict[str, Any]],
    ref: str,
    workflow: str = DEFAULT_WORKFLOW,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    scan_run_id: str = "",
) -> int:
    """Run ``gh workflow run`` once per issue. Returns the number of
    successful dispatches; raises SystemExit if any ticket is malformed or a
    dispatch fails."""
    dispatched = 0
    for issue in issues:
        ticket = (issue or {}).get("identifier")
        severity = (issue or {}).get("severity", "")
        if not ticket:
            raise SystemExit(f"::error::issue entry missing 'identifier': {issue}")
        cmd = [
            "gh",
            "workflow",
            "run",
            workflow,
            "--ref",
            ref,
            "-f",
            f"ticket={ticket}",
            "-f",
            f"severity={severity}",
        ]
        if scan_run_id:
            cmd += ["-f", f"scan_run_id={scan_run_id}"]
        print(f"Dispatching {workflow} for {ticket} (severity={severity or 'n/a'})")
        result = runner(cmd, check=False)
        if result.returncode != 0:
            raise SystemExit(
                f"::error::failed to dispatch {workflow} for {ticket} "
                f"(exit {result.returncode})"
            )
        dispatched += 1
    return dispatched


def main() -> int:
    issues = parse_issues(os.environ.get("NEW_ISSUES"))
    if not issues:
        print("No new issues to dispatch.")
        return 0
    ref = os.environ.get("GH_REF", "main")
    workflow = os.environ.get("VULN_TRIAGE_WORKFLOW", DEFAULT_WORKFLOW)
    n = dispatch(issues, ref, workflow, scan_run_id=os.environ.get("SCAN_RUN_ID", ""))
    print(f"Dispatched vuln-triage for {n} ticket(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
