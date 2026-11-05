#!/usr/bin/env python3
"""Shard the certify gate's unit tests across parallel runners.

``build-and-publish-app.yaml`` certifies every app by running its
``tests/unit`` before publish. On one 2-vCPU runner a large suite outgrows the
job's 30-minute cap, and publish waits on certify long after the image is
built. This script holds the two decisions the sharded layout needs, so each
branch has a test (``tests/test_certify_unit_shards.py``; see
``docs/standards/ci.md``). Sharding is opt-in: at the default of one shard
``certify`` calls ``run`` itself; an app that sets ``unit_test_shards`` (2-10)
gets one ``certify-unit`` leg per shard.

``run``
    One shard: pytest with coverage, plus pytest-split's ``--splits/--group``
    when there is more than one shard. Writes a result file and the shard's
    coverage data for the fan-in.
``verdict``
    Fan-in: read every shard's result file and decide the unit-test outcome
    the certification verdict enforces. A missing result counts as a failure —
    publish must never pass on evidence that was not produced.

Stdlib only: runs with the runner's system ``python3``.

Usage::

    python3 certify_unit_shards.py run --shard 2 --total 4 --coverage-source app \\
        --install-outcome success --results-dir ../certify-shard
    python3 certify_unit_shards.py verdict --results-dir certify-shards --total 4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

#: pytest exits 5 when it collects nothing. One shard of several can
#: legitimately be empty (more shards than tests), so it is recorded as
#: ``no_tests`` and passes — unless EVERY shard is empty, which means the whole
#: suite collected nothing. That failed the gate before sharding and still does,
#: as does a single shard that collects nothing.
PYTEST_NO_TESTS_COLLECTED = 5

UNIT_TESTS_DIR = "tests/unit"

#: The shard counts ``unit_test_shards`` accepts. The workflow's matrix table
#: has no entry outside this range, so no leg runs; the verdict must then fail
#: rather than read "no results" as "nothing to test".
MIN_SHARDS = 1
MAX_SHARDS = 10

Runner = Callable[[Sequence[str]], int]


def pytest_command(shard: int, total: int, coverage_source: str) -> list[str]:
    """The pytest invocation for one shard, run from the app checkout."""
    # --frozen: never re-lock. The install step synced every group, and it is
    # the only step with git auth for an app's private atlanhq deps.
    command = ["uv", "run", "--frozen", "--all-groups", "--with", "pytest-cov"]
    if total > 1:
        command += ["--with", "pytest-split"]
    command += [
        "pytest",
        UNIT_TESTS_DIR,
        f"--cov={coverage_source}",
        "--cov-report=term",
    ]
    if total > 1:
        command += [
            "--splits",
            str(total),
            "--group",
            str(shard),
            "--splitting-algorithm",
            "least_duration",
        ]
    return command


def run_shard(
    shard: int,
    total: int,
    coverage_source: str,
    install_outcome: str,
    results_dir: Path,
    app_dir: Path,
    runner: Runner,
) -> str:
    """Run one shard and record its outcome; return the outcome.

    Outcomes: ``success``, ``failure``, ``no_tests`` (an empty shard of
    several), ``skipped`` (no ``tests/unit``), or ``install_failed`` (app deps
    did not install, so nothing could run).
    """
    if install_outcome != "success":
        outcome = "install_failed"
    elif not (app_dir / UNIT_TESTS_DIR).is_dir():
        print(f"No {UNIT_TESTS_DIR}/ — skipping.")
        outcome = "skipped"
    else:
        code = runner(pytest_command(shard, total, coverage_source))
        if code == 0:
            outcome = "success"
        elif code == PYTEST_NO_TESTS_COLLECTED and total > 1:
            outcome = "no_tests"
        else:
            outcome = "failure"
        coverage_file = app_dir / ".coverage"
        if coverage_file.is_file():
            results_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(coverage_file), results_dir / f"coverage.shard-{shard}")

    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"shard-{shard}.json").write_text(
        json.dumps({"shard": shard, "outcome": outcome}), encoding="utf-8"
    )
    return outcome


def decide_verdict(results_dir: Path, total: int) -> tuple[str, list[str]]:
    """Fold every shard's outcome into one.

    Outcomes: ``success``, ``failure``, ``skipped`` (no ``tests/unit`` — passes;
    an app may have no unit tests) or ``install_failed`` (a shard's app deps did
    not install, so its tests never ran). The two non-runs are kept apart
    because the certification verdict treats them oppositely: an app with no
    tests passes, an app whose tests could not run fails closed.

    Returns the outcome and the reasons to annotate.
    """
    if not MIN_SHARDS <= total <= MAX_SHARDS:
        return "failure", [
            f"unit_test_shards must be {MIN_SHARDS} (default, no split) or "
            f"2..{MAX_SHARDS}, got {total}; no unit tests ran."
        ]

    outcomes: dict[int, str] = {}
    for path in sorted(results_dir.glob("shard-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            outcomes[int(record["shard"])] = str(record["outcome"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return "failure", [f"Unreadable shard result {path.name}: {exc}"]

    missing = [shard for shard in range(1, total + 1) if shard not in outcomes]
    if missing:
        return "failure", [
            f"No result from unit-test shard(s) {missing} — treated as failed."
        ]

    failed = [shard for shard, outcome in outcomes.items() if outcome == "failure"]
    if failed:
        return "failure", [f"Unit tests failed in shard(s) {failed} of {total}."]

    install_failed = sorted(
        shard for shard, outcome in outcomes.items() if outcome == "install_failed"
    )
    if install_failed:
        return "install_failed", [
            f"App dependencies did not install in unit-test shard(s) "
            f"{install_failed}, so their tests did not run."
        ]
    if all(outcome == "skipped" for outcome in outcomes.values()):
        return "skipped", []
    if all(outcome == "no_tests" for outcome in outcomes.values()):
        return "failure", [
            f"No unit tests were collected in any of the {total} shards."
        ]
    return "success", []


def _subprocess_runner(command: Sequence[str]) -> int:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(list(command), check=False).returncode


def _write_output(key: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")
    print(f"{key}={value}")


def main(argv: Sequence[str] | None = None, runner: Runner = _subprocess_runner) -> int:
    parser = argparse.ArgumentParser(description="Shard the certify gate's unit tests.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--shard", type=int, required=True)
    run.add_argument("--total", type=int, required=True)
    run.add_argument("--coverage-source", required=True)
    run.add_argument("--install-outcome", required=True)
    run.add_argument("--results-dir", type=Path, required=True)

    verdict = sub.add_parser("verdict")
    verdict.add_argument("--results-dir", type=Path, required=True)
    verdict.add_argument("--total", type=int, required=True)

    args = parser.parse_args(argv)

    if args.command == "run":
        outcome = run_shard(
            args.shard,
            args.total,
            args.coverage_source,
            args.install_outcome,
            args.results_dir.resolve(),
            Path.cwd(),
            runner,
        )
        print(f"Shard {args.shard}/{args.total}: {outcome}")
        return 1 if outcome == "failure" else 0

    outcome, reasons = decide_verdict(args.results_dir, args.total)
    for reason in reasons:
        level = "error" if outcome in ("failure", "install_failed") else "warning"
        print(f"::{level}::{reason}")
    _write_output("outcome", outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
