#!/usr/bin/env python3
"""Fail the dashboard run when a bounded-lock refusal outlived the reaper (FND-909).

Reads what the Renovate dashboard scanner already produced — no GitHub API calls,
no second fleet walk. ``renovate-dashboard.yaml`` runs the scanner every six hours
and writes ``fleet.json`` plus one ``repos/<slug>.json`` per repo; this reads that
output and turns one classification into a failing check.

Why this exists
---------------
The reaper step in ``renovate.yaml`` deletes a self-healing lock refusal on sight,
and by design it never fails its own job — Renovate has to run either way, so a
reaper outage costs a cycle of recovery latency rather than the lock refresh
itself. The consequence is that the outage is *silent*: the fleet run stays green
and a lock PR simply sits red in some repo nobody is watching, which is the
FND-909 freeze all over again.

``BlockingReason.BOUNDED_LOCK_REFUSAL_EXPIRED`` is exactly that state — a refusal
the reaper should already have cleared (see ``bounded_lock_refusal_state`` in the
conformance classifier for the two clocks that reach it). Any count above zero
means the reaper did not run, so it is an alarm rather than a dashboard tile.

What it deliberately does not alarm on
--------------------------------------
``BOUNDED_LOCK_REFUSAL_STANDING`` — a refusal stamped with a reason no amount of
waiting fixes (a broken interpreter, an unsatisfiable floor, a yanked-pin
rollback). The reaper leaves those alone on purpose and a human owns the branch.
They are printed, and they do not fail the run: a standing fault that a human is
legitimately still working through must not red a scheduled job every six hours,
or the alarm becomes noise and stops being read.

Usage::

    python3 renovate_refusal_alarm.py --out-dir /tmp/renovate-output
    python3 renovate_refusal_alarm.py --out-dir /tmp/renovate-output --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

# Mirrors conformance.renovate.models.BlockingReason. Kept as literals rather
# than an import because this script runs as a bare `python3` on the runner,
# outside the uv environment the scanner itself runs in.
EXPIRED = "bounded_lock_refusal_expired"
STANDING = "bounded_lock_refusal_standing"


def _load(path: str) -> Optional[dict]:
    """Parse one JSON file, or None if it is missing or malformed."""
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def offenders(out_dir: str, reason: str) -> list[dict]:
    """Every open PR in the scanner's per-repo output blocked for ``reason``.

    Read from ``repos/*.json`` rather than ``fleet.json`` because the aggregate
    carries counts alone. An alarm that says "1 frozen refusal" and cannot say
    where sends the reader back to the dashboard to find it by hand.
    """
    repos_dir = os.path.join(out_dir, "repos")
    try:
        names = sorted(os.listdir(repos_dir))
    except OSError:
        return []

    found: list[dict] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        report = _load(os.path.join(repos_dir, name))
        if report is None:
            print(f"::warning::unreadable repo report: {name}", file=sys.stderr)
            continue
        for pr in report.get("openPRs") or []:
            if pr.get("blockingReason") != reason:
                continue
            found.append(
                {
                    "repo": report.get("repo", name[: -len(".json")]),
                    "number": pr.get("number"),
                    "url": pr.get("url", ""),
                    "window": pr.get("lockRefusalWindow", ""),
                    "reason": pr.get("lockRefusalReason", ""),
                    "ageDays": pr.get("ageDays", 0),
                }
            )
    return found


def _describe(pr: dict) -> str:
    # prAge, not age: the classifier decides on the *branch head's* commit date,
    # which Renovate rewrites in place, so PR age is context and not the clock
    # anything here was judged against. Naming it plainly beats a reader assuming
    # the two are the same number.
    stamp = pr["reason"] or "unstamped"
    return (
        f"  {pr['repo']}#{pr['number']}  {stamp}"
        f"  window={pr['window'] or '-'}  prAge={pr['ageDays']}d\n"
        f"    {pr['url']}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        required=True,
        help="directory the renovate-scan CLI wrote (contains fleet.json, repos/)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    frozen = offenders(args.out_dir, EXPIRED)
    standing = offenders(args.out_dir, STANDING)

    if args.json:
        print(json.dumps({"frozen": frozen, "standing": standing}, indent=2))
    else:
        print(f"frozen self-healing refusals: {len(frozen)}")
        for pr in frozen:
            print(_describe(pr))
        print(f"standing faults (human-owned, not alarmed): {len(standing)}")
        for pr in standing:
            print(_describe(pr))

    if standing:
        # Visible, never fatal: these are red on purpose until a human clears them.
        print(
            f"::warning::{len(standing)} bounded-lock refusal(s) need a human — "
            "a standing fault does not clear on its own"
        )
    if frozen:
        print(
            f"::error::{len(frozen)} bounded-lock refusal(s) outlived the reaper — "
            "the reaper step in renovate.yaml is not clearing self-healing "
            "refusals (FND-909)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
