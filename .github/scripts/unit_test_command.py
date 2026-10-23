"""Build and run the SDK unit-test pytest command for one matrix leg.

Called by ``.github/actions/unit-tests``. The per-leg decisions — which uv
groups exist on this Python, whether the socket guard can be on, how a hung
test is interrupted, whether this leg measures coverage — live here rather
than in the action's ``run:`` block so each branch has a test
(``tests/test_unit_test_command.py``; see ``docs/standards/ci.md``).

Usage::

    python .github/scripts/unit_test_command.py \\
        --python-version 3.12 --runner-os Linux --coverage true --fail-under 85
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Per-test ceiling. The slowest test left in the unit tier runs ~10s on a
#: Windows runner; a hang should fail in two minutes with a traceback rather
#: than run to the job's 15-minute cap and report ``cancelled`` (FND-4238).
TEST_TIMEOUT_SECONDS = 120

#: What a healthy runner should take for the whole pytest step, start-up
#: included (FND-4304). Over budget is a warning, never a failure: a slow
#: runner is not a broken suite, and a red leg for it would be a flake.
STEP_BUDGET_SECONDS = {"Windows": 180.0}
DEFAULT_STEP_BUDGET_SECONDS = 120.0

#: A unit test slower than this (setup + call + teardown) is annotated on the
#: run, so a new slow test shows up on the PR that adds it.
SLOW_TEST_SECONDS = 5.0

#: Annotate at most this many slow tests, slowest first: GitHub shows only the
#: first few annotations of a step, and a wall of them is noise.
MAX_SLOW_TEST_ANNOTATIONS = 10

#: Python 3.14+ legs skip the dev group (libcst has no 3.14 support) and
#: the tests that need it, or pyatlan's pydantic-v1 compat (upstream fix:
#: atlan-python PR #893).
_PY314_IGNORES = (
    "--ignore=tests/unit/tools",
    "--ignore=tests/unit/clients/test_atlan_client.py",
    "--ignore=tests/unit/credentials/test_atlan_client.py",
)


def _python_minor(python_version: str) -> int:
    major, minor, *_ = python_version.split(".")
    if major != "3":
        raise ValueError(f"unsupported Python version {python_version!r}")
    return int(minor)


def build_command(
    *,
    python_version: str,
    runner_os: str,
    coverage: bool,
    fail_under: str,
    junit_xml: str | None = None,
) -> list[str]:
    """Return the argv for this leg's unit-test run."""
    py314 = _python_minor(python_version) >= 14
    windows = runner_os == "Windows"

    argv = ["uv", "run", "--frozen"]
    if py314:
        argv += ["--no-group", "dev"]
    argv += ["pytest", "--import-mode=importlib", "tests/"]
    if py314:
        argv += _PY314_IGNORES

    # -n auto: the suite was single-threaded, so a leg took 6-10 minutes while
    # the runner sat on 4 idle vCPUs (FND-961).
    #
    # --capture=no is deliberately absent. xdist ignores it in workers, and
    # leaving it on means a failing test's stdout is written to a worker
    # process nobody reads instead of being replayed in the failure report.
    # --log-cli-level is deliberately absent too: it cost 42% of wall time
    # under xdist, because every worker formats each record and ships it over
    # execnet, and it made a job log 20,000 lines.
    argv += ["-n", "auto", "--hypothesis-show-statistics"]

    # pytest-socket: on Linux/macOS the event loop uses os.pipe()/AF_UNIX for
    # its self-pipe, so --allow-unix-socket is sufficient. On Windows,
    # ProactorEventLoop uses AF_INET socket pairs internally; --disable-socket
    # would break asyncio entirely there, so Windows runs without the guard.
    if not windows:
        argv += ["--disable-socket", "--allow-unix-socket"]

    # SIGALRM interrupts the hung test and the worker carries on. Windows has
    # no SIGALRM, so it gets the thread method, which dumps every stack and
    # kills the xdist worker; xdist reports the test failed and starts another.
    argv += [
        f"--timeout={TEST_TIMEOUT_SECONDS}",
        f"--timeout-method={'thread' if windows else 'signal'}",
    ]

    argv += ["--durations=25", "--durations-min=1.0"]

    # Per-test timings for report_timing(): the durations table above stops at
    # 25 and is read by people, this is read by the budget check.
    if junit_xml is not None:
        argv += [f"--junitxml={junit_xml}"]

    # Coverage is measured on one leg: tracing costs ~20-30% of every leg's
    # wall time, and only one leg's report is ever read. --cov rather than
    # `coverage run -m pytest`: coverage only traces the parent process, so
    # under xdist the workers' coverage would be silently lost.
    if coverage:
        argv += [
            "--cov=application_sdk",
            "--cov-report=xml",
            "--cov-report=html",
            "--cov-report=term",
            f"--cov-fail-under={fail_under}",
        ]
    return argv


@dataclass(frozen=True)
class SlowTest:
    name: str
    seconds: float


def slow_tests(junit_xml: Path) -> list[SlowTest]:
    """Every test in *junit_xml* slower than :data:`SLOW_TEST_SECONDS`, slowest first."""
    found = [
        SlowTest(
            f"{case.get('classname')}::{case.get('name')}", float(case.get("time"))
        )
        for case in ET.parse(junit_xml).getroot().iter("testcase")
        if float(case.get("time", "0")) > SLOW_TEST_SECONDS
    ]
    return sorted(found, key=lambda t: t.seconds, reverse=True)


def report_timing(
    *,
    runner_os: str,
    step_seconds: float,
    junit_xml: Path,
    emit: Callable[[str], None],
    summary: Path | None,
) -> None:
    """Annotate an over-budget step and slow tests; never change the outcome.

    *emit* receives GitHub workflow commands (``::warning ...``), one per call.
    *summary* is ``$GITHUB_STEP_SUMMARY`` when the step has one.
    """
    budget = STEP_BUDGET_SECONDS.get(runner_os, DEFAULT_STEP_BUDGET_SECONDS)
    slow = slow_tests(junit_xml) if junit_xml.is_file() else []

    if step_seconds > budget:
        emit(
            f"::warning title=Unit tests over budget::the pytest step took "
            f"{step_seconds:.0f}s against a {budget:.0f}s budget on {runner_os} "
            f"(FND-4304)"
        )
    for test in slow[:MAX_SLOW_TEST_ANNOTATIONS]:
        emit(
            f"::warning title=Slow unit test::{test.name} took {test.seconds:.1f}s "
            f"(over {SLOW_TEST_SECONDS:.0f}s)"
        )

    if summary is None:
        return
    lines = [
        "### Unit test timing",
        "",
        f"pytest step: **{step_seconds:.0f}s** (budget {budget:.0f}s on {runner_os})",
        "",
    ]
    if not junit_xml.is_file():
        lines.append("No JUnit report was written, so per-test timings are missing.")
    elif slow:
        lines += [
            f"Tests over {SLOW_TEST_SECONDS:.0f}s:",
            "",
            "| seconds | test |",
            "| --: | -- |",
        ]
        lines += [f"| {t.seconds:.1f} | `{t.name}` |" for t in slow]
    else:
        lines.append(f"No test took over {SLOW_TEST_SECONDS:.0f}s.")
    with summary.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _parse_bool(value: str) -> bool:
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError(f"expected 'true' or 'false', got {value!r}")
    return value == "true"


def _run(argv: Sequence[str]) -> int:
    """The one side effect: run pytest. Stubbed in tests."""
    return subprocess.run(list(argv), check=False).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--runner-os", required=True)
    parser.add_argument("--coverage", required=True, type=_parse_bool)
    parser.add_argument("--fail-under", required=True)
    args = parser.parse_args(argv)

    junit_xml = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / (
        "unit-tests-junit.xml"
    )
    command = build_command(
        python_version=args.python_version,
        runner_os=args.runner_os,
        coverage=args.coverage,
        fail_under=args.fail_under,
        junit_xml=str(junit_xml),
    )
    # A report left by an earlier run on a reused runner would be read as this one's.
    junit_xml.unlink(missing_ok=True)
    print("+ " + " ".join(command), flush=True)
    started = time.monotonic()
    code = _run(command)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    report_timing(
        runner_os=args.runner_os,
        step_seconds=time.monotonic() - started,
        junit_xml=junit_xml,
        emit=lambda line: print(line, flush=True),
        summary=Path(summary) if summary else None,
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
