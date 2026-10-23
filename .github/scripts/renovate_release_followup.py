#!/usr/bin/env python3
"""Dispatch the release Renovate sweep, then re-dispatch the fleet repos it missed.

Invoked by the release fan-out jobs in tag-and-publish.yaml and
conformance-publish.yml, after wait_for_pypi_release.py (FND-4330).

WHY. A full-fleet sweep shortly after a release does not reliably move every
repo. On 2026-10-22 one repo's PyPI lookup returned a stale release list, so
its framework PR carried only the SDK, and another repo's `uv lock` resolved to
an unchanged lock, so it got no PR at all. Nothing looked at either again
until the next scheduled sweep, two to four hours later. This script closes
that gap within the hour: it waits for the sweep it dispatched, finds the
repos still behind, and dispatches a `repos`-scoped run for just those.

STEPS.
  1. Dispatch renovate.yaml with its live defaults, and get the run id back
     from the dispatch call (`return_run_details`). A failed dispatch prints
     the same warning the fan-out always has, and stops.
  2. Poll that run to completion (poll_dispatched_run.poll). If the run was
     cancelled, a newer live sweep superseded it (renovate.yaml coalesces live
     sweeps), and that run is a release fan-out with its own follow-up or the
     schedule. Either way verifying now would race it, so stop.
  3. Verify the fleet against the released versions, and dispatch a scoped run
     for the stragglers. At most `--max-redispatches` times, so it can't loop.

FLEET. Same definition as the Fleet Release Stragglers tracker:
discover_org_consumers.py's roster, minus soft-mode repos (they hold
first-party PRs for a human on purpose), minus repos with no rules on `main`
(nothing auto-merges there either).

STRAGGLER. A repo is behind when NEITHER its `main` uv.lock NOR the uv.lock on
its open `renovate/atlan-framework-dependencies` branch locks every expected
version. Versions come from uv.lock, never from PR titles: a title can name a
version the lock does not hold. A package is expected only if the repo's
`main` lock contains it at all, the release is final (Renovate does not offer
a pre-release to a repo on a final version), and, for the SDK and its api
package, the major matches (preset: SDK majors are an opt-in migration, while
conformance majors auto-merge).

Never fails the job for an operational problem: every such path is a warning
and exit 0, matching the fan-out's existing soft-fail. The scheduled sweep is
still the backstop.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))

import poll_dispatched_run  # noqa: E402
from discover_org_consumers import (  # noqa: E402
    DEFAULT_NAME_PATTERN,
    PRESET_MARKER,
    DiscoveryError,
    discover_fleet_with_modes,
)

SDK_REPO = "atlanhq/application-sdk"
WORKFLOW = "renovate.yaml"
FRAMEWORK_BRANCH = "renovate/atlan-framework-dependencies"

# Packages whose major releases the fleet adopts by hand (renovate-config
# default.json, SDK lane rule), so a repo on an older major is not a straggler.
MAJORS_OPT_IN = {"atlan-application-sdk", "atlan-application-sdk-api"}

FINAL_RELEASE = re.compile(r"^\d+(\.\d+)*$")

DISPATCH_FALLBACK = (
    "Could not trigger renovate.yaml — consumers will update on the next "
    "scheduled run."
)

GH_TIMEOUT_SECONDS = 60

# (args) -> (returncode, stdout, stderr), the discover_org_consumers.py seam.
RunFn = Callable[[list], tuple]


def _run_gh(args: list) -> tuple:
    """Run `gh` and return (returncode, stdout, stderr). Single test seam."""
    # Bounded, so a stalled request lands on the callers' warning paths instead
    # of holding the fan-out until the job timeout.
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return 1, "", f"gh timed out after {GH_TIMEOUT_SECONDS}s"
    return result.returncode, result.stdout, result.stderr


def dispatch(repos: list[str] | None, run: RunFn = _run_gh) -> str | None:
    """Dispatch renovate.yaml on main and return the new run's id, or None.

    `repos=None` is the live full-fleet sweep: no inputs, so it keeps
    renovate.yaml's defaults and its shared concurrency group. A list scopes
    the run, which puts it on a run-unique group that cannot cancel, or be
    cancelled by, a live sweep.
    """
    args = [
        "api",
        "--method",
        "POST",
        f"repos/{SDK_REPO}/actions/workflows/{WORKFLOW}/dispatches",
        "-f",
        "ref=main",
        "-F",
        "return_run_details=true",
    ]
    if repos is not None:
        args += ["-f", f"inputs[repos]={json.dumps(repos)}"]
    code, out, err = run(args)
    if code != 0:
        print(f"::warning::{DISPATCH_FALLBACK} ({err.strip()})")
        return None
    try:
        run_id = json.loads(out).get("workflow_run_id")
    except (json.JSONDecodeError, AttributeError):
        run_id = None
    if not run_id:
        # The sweep was dispatched; only the follow-up is lost.
        print(
            "::warning::renovate.yaml was dispatched but the response carried no "
            f"workflow_run_id ({out.strip()[:200]!r}); skipping the post-sweep check."
        )
        return None
    print(f"Dispatched renovate.yaml run {run_id} (repos={repos or 'full fleet'})")
    return str(run_id)


def has_rules_on_main(repo: str, run: RunFn = _run_gh) -> bool:
    """True if any ruleset applies to `main`. Unreadable counts as True: a
    wrongly included repo costs one scoped Renovate pass, a wrongly excluded
    one is the miss this script exists to catch."""
    code, out, err = run(["api", f"repos/{repo}/rules/branches/main"])
    if code != 0:
        print(
            f"::warning::could not read rules on {repo}@main ({err.strip()}); keeping it"
        )
        return True
    try:
        return bool(json.loads(out))
    except json.JSONDecodeError:
        return True


def read_lock(repo: str, ref: str, run: RunFn = _run_gh) -> dict[str, str] | None:
    """{package: version} from `repo`'s uv.lock at `ref`; None if absent (404).

    Any other failure raises RuntimeError, which the caller treats as "cannot
    show the repo is current".
    """
    code, out, err = run(
        [
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo}/contents/uv.lock?ref={ref}",
        ]
    )
    if code != 0:
        if "HTTP 404" in err:
            return None
        raise RuntimeError(f"reading {repo}/uv.lock@{ref} failed: {err.strip()}")
    try:
        lock = tomllib.loads(out)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"{repo}/uv.lock@{ref} is not TOML: {exc}") from exc
    return {
        str(p["name"]): str(p["version"])
        for p in lock.get("package", [])
        if "name" in p and "version" in p
    }


def expected_versions(
    released: dict[str, str], main_lock: dict[str, str]
) -> dict[str, str]:
    """The subset of `released` this repo should now lock. See module docstring."""
    expected = {}
    for name, version in released.items():
        locked = main_lock.get(name)
        if locked is None or not FINAL_RELEASE.match(version):
            continue
        if name in MAJORS_OPT_IN and locked.split(".")[0] != version.split(".")[0]:
            continue
        expected[name] = version
    return expected


def _locks_all(lock: dict[str, str] | None, expected: dict[str, str]) -> bool:
    return lock is not None and all(lock.get(n) == v for n, v in expected.items())


def is_straggler(repo: str, released: dict[str, str], run: RunFn = _run_gh) -> bool:
    try:
        main_lock = read_lock(repo, "main", run=run)
        if main_lock is None:
            return False  # no uv.lock: nothing for the framework lane to move
        expected = expected_versions(released, main_lock)
        if _locks_all(main_lock, expected):
            return False
        return not _locks_all(read_lock(repo, FRAMEWORK_BRANCH, run=run), expected)
    except RuntimeError as exc:
        print(f"::warning::{exc}; re-dispatching {repo} to be safe")
        return True


def find_stragglers(
    released: dict[str, str], owner: str, run: RunFn = _run_gh
) -> list[str]:
    """Fleet repos still behind `released`. Raises DiscoveryError."""
    repos, modes = discover_fleet_with_modes(
        owner, DEFAULT_NAME_PATTERN, PRESET_MARKER, {SDK_REPO}, run=run
    )
    monitored = [
        r for r in repos if modes.get(r) != "soft" and has_rules_on_main(r, run=run)
    ]
    print(
        f"Checking {len(monitored)} monitored fleet repos (of {len(repos)} discovered)"
    )
    return [r for r in monitored if is_straggler(r, released, run=run)]


def _summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def followup(
    released: dict[str, str],
    *,
    owner: str = "atlanhq",
    max_redispatches: int = 2,
    retry_delay_seconds: int = 120,
    run_timeout_seconds: int = 3600,
    run: RunFn = _run_gh,
    poll: Callable[..., tuple[str, str]] = poll_dispatched_run.poll,
    sleep: Callable[[float], None] | None = None,
) -> list[str]:
    """Run the dispatch → wait → verify → re-dispatch loop.

    Returns the stragglers left at the end ([] when the fleet caught up, or
    when the check was skipped).
    """
    sleep = sleep or time.sleep

    run_id = dispatch(None, run=run)
    if run_id is None:
        return []
    if not any(FINAL_RELEASE.match(v) for v in released.values()):
        print("Only pre-releases in this release; no fleet repo is expected to move.")
        return []

    stragglers: list[str] = []
    for attempt in range(max_redispatches + 1):
        status, conclusion = poll(SDK_REPO, run_id, timeout_seconds=run_timeout_seconds)
        if conclusion == "cancelled":
            print(
                f"::notice::Renovate run {run_id} was cancelled by a newer sweep; "
                "that sweep's own fan-out, or the schedule, covers the fleet."
            )
            return []
        # Only a completed run is safe to verify behind: poll() returns on any
        # status outside its pending set, and timeout/unreadable/an unknown
        # status may all mean the sweep is still pushing branches.
        if status != "completed":
            print(
                f"::warning::Renovate run {run_id} ended polling with status "
                f"{status!r}; skipping the post-sweep check."
            )
            return []

        try:
            stragglers = find_stragglers(released, owner, run=run)
        except DiscoveryError as exc:
            print(
                f"::warning::Fleet discovery failed ({exc}); skipping the post-sweep check."
            )
            return []

        _summary(
            f"- After run {run_id}: "
            + (", ".join(f"`{r}`" for r in stragglers) or "no stragglers")
        )
        if not stragglers:
            print(
                "Every monitored fleet repo has the release (on main or its open framework PR)."
            )
            return []
        if attempt == max_redispatches:
            break

        print(f"{len(stragglers)} straggler(s): {', '.join(stragglers)}")
        sleep(retry_delay_seconds)
        run_id = dispatch(stragglers, run=run)
        if run_id is None:
            return stragglers

    print(
        f"::warning::{len(stragglers)} fleet repo(s) still lack the release after "
        f"{max_redispatches} scoped re-run(s): {', '.join(stragglers)}"
    )
    return stragglers


def main(argv: list[str] | None = None, run: RunFn = _run_gh) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--packages",
        required=True,
        help='JSON {"package": "version"}: wait_for_pypi_release.py\'s output.',
    )
    parser.add_argument("--owner", default="atlanhq")
    parser.add_argument("--max-redispatches", type=int, default=2)
    parser.add_argument("--retry-delay-seconds", type=int, default=120)
    args = parser.parse_args(argv)

    try:
        released = json.loads(args.packages)
    except json.JSONDecodeError:
        released = None
    if not isinstance(released, dict) or not released:
        # Still dispatch: a broken wait step must not cost the fleet its sweep.
        print(
            f"::warning::--packages is not a JSON object ({args.packages!r}); dispatching without the post-sweep check."
        )
        dispatch(None, run=run)
        return 0

    followup(
        {str(k): str(v) for k, v in released.items()},
        owner=args.owner,
        max_redispatches=args.max_redispatches,
        retry_delay_seconds=args.retry_delay_seconds,
        run=run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
