"""python -m vuln_triage --repo owner/name --root <checkout> --ticket FND-123 [...]"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .run import Context, Deps, run


def parse_bool(value: str) -> bool:
    """`true`/`false` as the workflow's boolean input renders them; nothing else."""
    v = value.strip().lower()
    if v == "true":
        return True
    if v == "false":
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {value!r}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vuln_triage")
    p.add_argument("--repo", required=True)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--ticket", default="", help="required unless --selftest")
    p.add_argument("--severity", default="")
    p.add_argument("--scan-run-id", default="")
    p.add_argument(
        "--scan-dir", type=Path, help="pre-downloaded Trivy JSON (skips download)"
    )
    p.add_argument("--run-url", default="")
    p.add_argument("--run-id", default="")
    # `--dry-run` alone, or `--dry-run true|false` so the workflow can pass its
    # boolean input straight through without branching shell. Anything else is an
    # error: a typo in the no-write switch must never fall through to a live run.
    p.add_argument(
        "--dry-run",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool,
        help="run every step and check, print the comment; push, open, comment nothing",
    )
    p.add_argument(
        "--selftest",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool,
        help="fake ticket + scan (selftest.py); draft PRs opened then closed",
    )
    a = p.parse_args(argv)
    if not a.selftest and not a.ticket.strip():
        p.error("--ticket is required (or pass --selftest)")
    ctx = Context(
        repo=a.repo,
        root=a.root.resolve(),
        ticket=a.ticket.strip(),
        severity=a.severity.strip(),
        scan_run_id=a.scan_run_id.strip(),
        scan_dir=a.scan_dir.resolve() if a.scan_dir else None,
        run_url=a.run_url,
        run_id=a.run_id,
        dry_run=a.dry_run,
        selftest=a.selftest,
    )
    run(ctx, Deps(runner=subprocess.run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
