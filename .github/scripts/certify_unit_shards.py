#!/usr/bin/env python3
"""Shard the certify gate's unit tests across parallel runners.

``build-and-publish-app.yaml`` certifies every app by running its
``tests/unit`` before publish. On one 2-vCPU runner a large suite outgrows the
job's 30-minute cap, and publish waits on certify long after the image is
built. This script holds the three decisions the sharded layout needs, so each
branch has a test (``tests/test_certify_unit_shards.py``; see
``docs/standards/ci.md``):

``plan``
    How many shards. An explicit ``--override`` wins; otherwise the app's
    pytest-split ``.test_durations`` file sizes it (one shard per
    ``TARGET_SECONDS_PER_SHARD`` of recorded ``tests/unit`` time). An app with
    no durations file gets one shard — the same single job as before.
``run``
    One shard: pytest with coverage, plus pytest-split's ``--splits/--group``
    when there is more than one shard. Writes a result file and the shard's
    coverage data for the fan-in.
``verdict``
    Fan-in: read every shard's result file and decide the unit-test outcome
    the certification verdict enforces. A missing result counts as a failure —
    publish must never pass on evidence that was not produced.

Stdlib only: ``plan`` runs on ``ubuntu-slim`` with the system ``python3``.

Usage::

    python3 certify_unit_shards.py plan --durations app/.test_durations --override 0
    python3 certify_unit_shards.py run --shard 2 --total 4 --coverage-source app \\
        --install-outcome success --results-dir ../certify-shard
    python3 certify_unit_shards.py verdict --results-dir certify-shards --total 4
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

#: Recorded serial seconds of ``tests/unit`` one shard should carry. A
#: standard runner runs ~5 minutes of serial test time well inside the job cap
#: even without xdist, and leaves the per-shard setup (checkout + ``uv sync``,
#: ~1 billed minute) a small share of each leg.
TARGET_SECONDS_PER_SHARD = 300.0

#: Ceiling on legs. Past this the slowest single test, not the shard count,
#: is the floor, while every extra leg still bills its setup minute.
MAX_SHARDS = 10

#: pytest exits 5 when it collects nothing. A shard can legitimately be empty
#: (more shards than tests), so that is a pass, not a failure.
PYTEST_NO_TESTS_COLLECTED = 5

UNIT_TESTS_DIR = "tests/unit"

Runner = Callable[[Sequence[str]], int]


def plan_shards(durations_path: Path, override: int) -> int:
    """Return the shard count: ``override`` when set, else sized from durations."""
    if override < 0 or override > MAX_SHARDS:
        raise ValueError(
            f"unit_test_shards must be 0 (auto) or 1..{MAX_SHARDS}, got {override}"
        )
    if override:
        return override
    try:
        durations = json.loads(durations_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 1
    except (OSError, ValueError) as exc:
        print(
            f"::warning::Unreadable {durations_path} ({exc}); running unit tests as one shard."
        )
        return 1
    if not isinstance(durations, dict):
        print(
            f"::warning::{durations_path} is not a pytest-split durations map; one shard."
        )
        return 1
    seconds = sum(
        float(value)
        for node_id, value in durations.items()
        if str(node_id).startswith(f"{UNIT_TESTS_DIR}/")
        and isinstance(value, (int, float))
    )
    return max(1, min(MAX_SHARDS, math.ceil(seconds / TARGET_SECONDS_PER_SHARD)))


def pytest_command(shard: int, total: int, coverage_source: str) -> list[str]:
    """The pytest invocation for one shard, run from the app checkout."""
    command = ["uv", "run", "--all-groups", "--with", "pytest-cov"]
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

    Outcomes: ``success``, ``failure``, ``skipped`` (no ``tests/unit``), or
    ``install_failed`` (app deps did not install, so nothing could run).
    """
    if install_outcome != "success":
        outcome = "install_failed"
    elif not (app_dir / UNIT_TESTS_DIR).is_dir():
        print(f"No {UNIT_TESTS_DIR}/ — skipping.")
        outcome = "skipped"
    else:
        code = runner(pytest_command(shard, total, coverage_source))
        outcome = "success" if code in (0, PYTEST_NO_TESTS_COLLECTED) else "failure"
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
    """Fold every shard's outcome into one: ``success``, ``failure`` or ``skipped``.

    Returns the outcome and the reasons to annotate. ``skipped`` keeps the
    gate's existing meaning: no unit tests to run, or app deps that did not
    install (warn-only, as before sharding).
    """
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

    if any(outcome == "install_failed" for outcome in outcomes.values()):
        return "skipped", ["App dependencies did not install in a unit-test shard."]
    if all(outcome == "skipped" for outcome in outcomes.values()):
        return "skipped", []
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

    plan = sub.add_parser("plan")
    plan.add_argument("--durations", type=Path, required=True)
    plan.add_argument("--override", type=int, default=0)

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

    if args.command == "plan":
        try:
            total = plan_shards(args.durations, args.override)
        except ValueError as exc:
            print(f"::error::{exc}")
            return 1
        _write_output("total", str(total))
        _write_output("shards", json.dumps(list(range(1, total + 1))))
        return 0

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
        level = "error" if outcome == "failure" else "warning"
        print(f"::{level}::{reason}")
    _write_output("outcome", outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
