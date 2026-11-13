"""Build and run the SDK unit-test pytest command for one matrix leg.

Called by ``.github/actions/unit-tests``. The per-leg decisions — which uv
groups exist on this Python, whether the socket guard can be on, how a hung
test is interrupted, whether this leg measures coverage — live here rather
than in the action's ``run:`` block so each branch has a test
(``tests/test_unit_test_command.py``; see ``docs/standards/ci.md``).

On the coverage leg it also holds every top-level ``application_sdk``
subpackage to a floor of its own, read from ``coverage.xml``, so one package
cannot sink far below the global gate under a passing total (FND-4678).

Usage::

    python .github/scripts/unit_test_command.py \\
        --python-version 3.12 --runner-os Linux --coverage true --fail-under 85

    # The dependency-floor leg: the same suite with pandas and pyarrow at the
    # lowest versions the SDK publishes, instead of the locked ones.
    python .github/scripts/unit_test_command.py \\
        --python-version 3.11 --runner-os Linux --coverage false --fail-under 85 \\
        --dependency-floors pandas,pyarrow
"""

from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
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

#: The uv group Python 3.14+ legs skip: libcst, for tools/migrate_v3 (once
#: without 3.14 support). Only this group, so the rest of the dev group
#: still installs there.
PY314_SKIPPED_GROUP = "migrate-v3"

#: Python 3.14+ legs skip the tests that need :data:`PY314_SKIPPED_GROUP`,
#: or pyatlan's pydantic-v1 compat (upstream fix: atlan-python PR #893).
_PY314_IGNORES = (
    "--ignore=tests/unit/tools",
    "--ignore=tests/unit/clients/test_atlan_client.py",
    "--ignore=tests/unit/credentials/test_atlan_client.py",
)


#: The extra whose published floors the dependency-floor leg installs. pandas
#: and pyarrow reach apps through it; the [incremental] and [pandas] extras
#: carry the same pins, and tests/unit/test_api_distribution.py holds
#: packages/api to them.
FLOOR_EXTRA = "sql"

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: Where ``--cov-report=xml`` writes, relative to the checkout. The PR coverage
#: comment and the coverage artifact read the same file.
COVERAGE_XML = Path("coverage.xml")

#: The pyproject table holding per-package floors that differ from the global
#: one: ``{"<subpackage>" = <percent>}``.
PACKAGE_FLOORS_TABLE = ("tool", "application-sdk", "coverage", "package-floors")

#: The group for modules directly under ``application_sdk/``.
ROOT_PACKAGE = "(top level)"

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_FLOOR_RE = re.compile(r">=\s*([^,;\s]+)")


class FloorError(ValueError):
    """A requested package has no ``>=`` floor in :data:`FLOOR_EXTRA`."""


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dependency_floors(
    packages: Sequence[str], pyproject: Path = _PYPROJECT
) -> list[str]:
    """``name==floor`` for each of *packages*, read from :data:`FLOOR_EXTRA`.

    Read from pyproject.toml rather than written into the workflow, so the leg
    tests the floor the SDK actually publishes and moves when the floor does.
    A package that is missing from the extra, or has no ``>=`` bound there, is
    an error: a leg that silently ran at the locked version would pass while
    testing nothing it claims to.
    """
    extras = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    floors: dict[str, str] = {}
    for spec in extras.get(FLOOR_EXTRA, []):
        name = _NAME_RE.match(spec)
        floor = _FLOOR_RE.search(spec)
        if name and floor:
            floors[_canonical(name.group(1))] = floor.group(1)

    pins: list[str] = []
    for package in packages:
        floor = floors.get(_canonical(package))
        if floor is None:
            raise FloorError(
                f"{package!r} has no '>=' floor in the [{FLOOR_EXTRA}] extra of "
                f"{pyproject}"
            )
        pins.append(f"{package}=={floor}")
    return pins


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
    floor_pins: Sequence[str] = (),
) -> list[str]:
    """Return the argv for this leg's unit-test run.

    *floor_pins* (``name==version``, from :func:`dependency_floors`) are
    installed over the locked environment with ``uv run --with``; the lock
    itself is still ``--frozen``.
    """
    py314 = _python_minor(python_version) >= 14
    windows = runner_os == "Windows"

    argv = ["uv", "run", "--frozen"]
    if py314:
        argv += ["--no-group", PY314_SKIPPED_GROUP]
    for pin in floor_pins:
        argv += ["--with", pin]
    # With --with, uv runs an overlay interpreter on top of the project venv.
    # The bare `pytest` console script is the venv's, and its shebang starts
    # the venv's own python, so the floor pins would never be imported and the
    # leg would pass on the locked versions while claiming the floors.
    # `python -m pytest` runs under the overlay interpreter.
    argv += ["python", "-m", "pytest"] if floor_pins else ["pytest"]
    argv += ["--import-mode=importlib", "tests/"]
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


class CoverageFloorError(ValueError):
    """The per-package floor table, or the report it is checked against, is unusable."""


@dataclass(frozen=True)
class PackageCoverage:
    package: str
    statements: int
    covered: int

    @property
    def percent(self) -> float:
        return 100.0 * self.covered / self.statements if self.statements else 100.0


def package_floors(pyproject: Path = _PYPROJECT) -> dict[str, float]:
    """The per-package floors that differ from the global one."""
    table: object = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    for key in PACKAGE_FLOORS_TABLE:
        if not isinstance(table, dict) or key not in table:
            return {}
        table = table[key]
    if not isinstance(table, dict):
        raise CoverageFloorError(f"[{'.'.join(PACKAGE_FLOORS_TABLE)}] is not a table")
    floors: dict[str, float] = {}
    for package, floor in table.items():
        if isinstance(floor, bool) or not isinstance(floor, (int, float)):
            raise CoverageFloorError(
                f"floor for {package!r} must be a number, got {floor!r}"
            )
        if not 0 <= floor <= 100:
            raise CoverageFloorError(f"floor for {package!r} is not a percentage")
        floors[package] = float(floor)
    return floors


def _package_of(filename: str) -> str:
    """Top-level ``application_sdk`` subpackage of a ``coverage.xml`` filename.

    coverage.py writes filenames relative to the ``source`` directory
    (``clients/sql.py``); the ``application_sdk/`` prefix is stripped in case a
    report was written from the checkout root instead.
    """
    parts = Path(filename.replace("\\", "/")).parts
    if parts and parts[0] == "application_sdk":
        parts = parts[1:]
    return parts[0] if len(parts) > 1 else ROOT_PACKAGE


def coverage_by_package(coverage_xml: Path) -> list[PackageCoverage]:
    """Line coverage per top-level subpackage, from a Cobertura ``coverage.xml``.

    Counted from each file's ``<line>`` elements rather than coverage.py's
    ``<package>`` rates, which are per directory: ``execution`` and
    ``execution/_temporal`` are separate packages there.
    """
    totals: dict[str, list[int]] = {}
    for cls in ET.parse(coverage_xml).getroot().iter("class"):
        counts = totals.setdefault(_package_of(cls.get("filename", "")), [0, 0])
        for line in cls.iter("line"):
            counts[0] += 1
            counts[1] += int(line.get("hits", "0")) > 0
    return [
        PackageCoverage(package, statements, covered)
        for package, (statements, covered) in sorted(totals.items())
    ]


def _shown(percent: float) -> str:
    """*percent* to two places, truncated so a miss never displays as its floor."""
    return f"{math.floor(percent * 100) / 100:.2f}"


def check_package_floors(
    *,
    measured: Sequence[PackageCoverage],
    floors: dict[str, float],
    default_floor: float,
    emit: Callable[[str], None],
    summary: Path | None,
) -> bool:
    """Hold each package to its floor (or *default_floor*); False when any misses.

    A floor naming a package the report does not have is a failure too: a
    renamed package would otherwise drop its floor without anyone noticing.
    """
    ok = True
    seen = {m.package for m in measured}
    for package in sorted(set(floors) - seen):
        ok = False
        emit(
            f"::error title=Coverage floor::'{package}' has a floor in "
            f"[{'.'.join(PACKAGE_FLOORS_TABLE)}] but no coverage was measured "
            f"for it; remove or rename the entry"
        )

    rows: list[str] = []
    for m in measured:
        floor = floors.get(m.package, default_floor)
        # Unrounded: 6,803/8,004 is 84.995%, which rounds to 85.00 but is
        # below an 85% floor.
        passed = m.percent >= floor
        if not passed:
            ok = False
            emit(
                f"::error title=Coverage floor::application_sdk/{m.package} is at "
                f"{_shown(m.percent)}% against its {floor:g}% floor "
                f"({m.covered}/{m.statements} lines)"
            )
        rows.append(
            f"| `{m.package}` | {_shown(m.percent)} | {floor:g} | {m.statements} "
            f"| {'' if passed else 'below floor'} |"
        )

    if summary is not None:
        lines = [
            "### Coverage by package",
            "",
            f"Floor is {default_floor:g}% unless listed in "
            f"`[{'.'.join(PACKAGE_FLOORS_TABLE)}]` in pyproject.toml.",
            "",
            "| package | % | floor | lines | |",
            "| -- | --: | --: | --: | -- |",
            *rows,
        ]
        with summary.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return ok


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
    parser.add_argument(
        "--dependency-floors",
        default="",
        help="comma-separated packages to install at their published floor",
    )
    args = parser.parse_args(argv)

    packages = [p.strip() for p in args.dependency_floors.split(",") if p.strip()]
    try:
        floor_pins = dependency_floors(packages)
    except FloorError as exc:
        print(f"::error title=Dependency floor::{exc}", flush=True)
        return 2
    # Read before the run, so a malformed table fails in seconds, not after it.
    try:
        coverage_floors = package_floors() if args.coverage else {}
    except CoverageFloorError as exc:
        print(f"::error title=Coverage floor::{exc}", flush=True)
        return 2

    junit_xml = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / (
        "unit-tests-junit.xml"
    )
    command = build_command(
        python_version=args.python_version,
        runner_os=args.runner_os,
        coverage=args.coverage,
        fail_under=args.fail_under,
        junit_xml=str(junit_xml),
        floor_pins=floor_pins,
    )
    # A report left by an earlier run on a reused runner would be read as this one's.
    junit_xml.unlink(missing_ok=True)
    if args.coverage:
        COVERAGE_XML.unlink(missing_ok=True)
    print("+ " + " ".join(command), flush=True)
    started = time.monotonic()
    code = _run(command)
    summary_env = os.environ.get("GITHUB_STEP_SUMMARY")
    summary = Path(summary_env) if summary_env else None

    def emit(line: str) -> None:
        print(line, flush=True)

    report_timing(
        runner_os=args.runner_os,
        step_seconds=time.monotonic() - started,
        junit_xml=junit_xml,
        emit=emit,
        summary=summary,
    )
    # A failed run has already failed the leg, and its report may be partial.
    if code != 0 or not args.coverage:
        return code
    if not COVERAGE_XML.is_file():
        emit(f"::error title=Coverage floor::pytest wrote no {COVERAGE_XML}")
        return 1
    passed = check_package_floors(
        measured=coverage_by_package(COVERAGE_XML),
        floors=coverage_floors,
        default_floor=float(args.fail_under),
        emit=emit,
        summary=summary,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
