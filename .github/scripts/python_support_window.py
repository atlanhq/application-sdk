#!/usr/bin/env python3
"""The SDK's supported Python window, and a probe of the next Python.

The root ``pyproject.toml`` declares the window as ``requires-python =
">=3.<floor>,<3.<cap>"``. The cap is there because the SDK cannot install on
the next interpreter until every binary dependency ships a wheel for it (on
3.15, ``rocksdict`` had no cp315 wheel and no sdist, FND-5476). Without the
cap a 3.15 user got an unsatisfiable-resolution error naming ``rocksdict``
rather than "the SDK does not support 3.15".

Two subcommands:

- ``print-window``  print the supported ``major.minor`` versions, one per
                    line, and the first unsupported one as ``next=<version>``.
- ``probe-next``    resolve every dependency the SDK declares (core and every
                    extra) for the first unsupported Python on the container
                    platform, and report which ones cannot install there. It
                    writes a Markdown report to ``$GITHUB_STEP_SUMMARY`` when
                    that is set and always exits 0: it is a status report for
                    deciding when to raise the cap, not a gate. Exit 2 means
                    the probe itself could not run.

``.github/scripts/tests/test_python_support_window.py`` keeps the classifiers,
the CI unit matrix and the Renovate Python cap equal to this window, so raising
the cap is one deliberate change across all of them.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from _gha import append_summary

# ">=3.11,<3.15" (uv writes ">=3.11, <3.15" in the lock). Anything else is a
# shape this script does not understand, and it fails rather than guessing.
_WINDOW_RE = re.compile(r"^\s*>=\s*3\.(\d+)\s*,\s*<\s*3\.(\d+)\s*$")

# The runtime container's platform: the probe answers "would the image build".
PROBE_PLATFORM = "x86_64-manylinux_2_28"

# Each uv call does index I/O. One call may take UV_TIMEOUT_S; the whole probe
# stops starting calls after PROBE_BUDGET_S. Together they stay inside the
# five-minute timeout of the job that runs this, so a stalled index ends the
# probe with an "incomplete" report instead of the runner killing the job.
UV_TIMEOUT_S = 60.0
PROBE_BUDGET_S = 180.0

Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


class ProbeIncomplete(Exception):
    """The probe ran out of time before it could name every blocker."""


def _repo_root() -> Path:
    # <repo>/.github/scripts/python_support_window.py -> <repo>
    return Path(__file__).resolve().parents[2]


def parse_window(requires_python: str) -> list[str]:
    """Return the supported ``major.minor`` versions for a capped specifier.

    Raises ``ValueError`` for an uncapped or otherwise unexpected specifier.
    """
    match = _WINDOW_RE.match(requires_python)
    if not match:
        raise ValueError(
            f"requires-python {requires_python!r} is not of the form "
            "'>=3.X,<3.Y'; the SDK caps the Python it supports (FND-5476)"
        )
    floor, cap = int(match.group(1)), int(match.group(2))
    if cap <= floor:
        raise ValueError(f"requires-python {requires_python!r} allows no version")
    return [f"3.{minor}" for minor in range(floor, cap)]


def next_version(window: Sequence[str]) -> str:
    """The first ``major.minor`` above ``window``."""
    major, minor = window[-1].split(".")
    return f"{major}.{int(minor) + 1}"


Project = Mapping[str, object]


def load_project(pyproject: Path) -> Project:
    """The ``[project]`` table of ``pyproject``."""
    table = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    if not isinstance(table, dict):
        raise ValueError(f"{pyproject} has no [project] table")
    return cast("Project", table)


def _str_list(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in cast("list[object]", value)
    ):
        raise ValueError(f"{what} is not a list of strings")
    return cast("list[str]", value)


def requires_python(project: Project) -> str:
    value = project.get("requires-python")
    if not isinstance(value, str):
        raise ValueError("[project] has no requires-python string")
    return value


def classifiers(project: Project) -> list[str]:
    return _str_list(project.get("classifiers", []), "classifiers")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    return _normalize(re.split(r"[\s\[<>=!~;(@]", requirement.strip(), maxsplit=1)[0])


def _all_requirements(project: Project) -> list[str]:
    """Core dependencies plus every extra's, in declared order."""
    requirements = list(_str_list(project.get("dependencies", []), "dependencies"))
    extras = project.get("optional-dependencies", {})
    if not isinstance(extras, dict):
        raise ValueError("optional-dependencies is not a table")
    for extra, group in cast("dict[str, object]", extras).items():
        requirements += _str_list(group, f"optional-dependencies.{extra}")
    return requirements


def _table(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{what} is not a table")
    return cast("dict[str, object]", value)


def local_packages(pyproject: Path) -> dict[str, Path]:
    """Packages this repo builds itself: name -> its pyproject.toml.

    Read from ``[tool.uv.sources]``: a ``path`` source names its directory, a
    ``workspace`` source is found among ``[tool.uv.workspace] members``. These
    release in lockstep with the SDK and carry the same requires-python cap,
    so the probe must look through them to their third-party dependencies
    rather than resolve them (their cap would refuse the target Python).
    """
    root = pyproject.parent
    tool = _table(
        tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}), "[tool]"
    )
    uv = _table(tool.get("uv", {}), "[tool.uv]")
    sources = _table(uv.get("sources", {}), "[tool.uv.sources]")
    members: list[Path] = []
    for pattern in _str_list(
        _table(uv.get("workspace", {}), "[tool.uv.workspace]").get("members", []),
        "workspace members",
    ):
        members += [m for m in root.glob(pattern) if (m / "pyproject.toml").is_file()]
    by_name = {
        _normalize(str(load_project(m / "pyproject.toml")["name"])): m
        / "pyproject.toml"
        for m in members
    }
    found: dict[str, Path] = {}
    for name, raw in sources.items():
        source = _table(raw, f"[tool.uv.sources] {name}")
        path = source.get("path")
        if isinstance(path, str) and (root / path / "pyproject.toml").is_file():
            found[_normalize(name)] = root / path / "pyproject.toml"
        elif source.get("workspace") is True and _normalize(name) in by_name:
            found[_normalize(name)] = by_name[_normalize(name)]
    return found


def declared_requirements(pyproject: Path) -> list[str]:
    """Every third-party requirement the SDK declares, deduplicated, in order.

    Core dependencies and every extra, of the SDK and, transitively, of each
    package this repo builds itself (``local_packages``), which are replaced
    by their own requirements. Self-references such as
    ``atlan-application-sdk[sql]`` are dropped likewise: the probe resolves
    what the SDK pulls in, not the SDK, whose cap would refuse the target
    Python outright.
    """
    local = local_packages(pyproject)
    root = load_project(pyproject)
    local[_normalize(str(root["name"]))] = pyproject
    seen: dict[str, None] = {}
    visited: set[Path] = set()
    queue = [pyproject]
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for requirement in _all_requirements(load_project(current)):
            name = _requirement_name(requirement)
            if name in local:
                queue.append(local[name])
                continue
            seen.setdefault(requirement.strip(), None)
    return list(seen)


@dataclass(frozen=True)
class Blocker:
    requirement: str
    reason: str


_NO_WHEEL_RE = re.compile(
    r"(\S+ has no wheels with a matching Python ABI tag(?: \([^)]*\))?)"
)


def _reason(stderr: str) -> str:
    """The clause of uv's output that says why, or its first non-empty line."""
    no_wheel = _NO_WHEEL_RE.search(stderr)
    if no_wheel:
        return no_wheel.group(1)
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("cause:"):
            return line.removeprefix("cause:").strip()[:300]
    return lines[0][:300] if lines else "uv exited non-zero with no output"


def _compile(
    requirements: Sequence[str],
    python: str,
    runner: Runner,
    workdir: Path,
    deadline: float,
) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeIncomplete(f"the {PROBE_BUDGET_S:.0f}s probe budget ran out")
    req_file = workdir / "requirements.in"
    req_file.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    return runner(
        [
            "uv",
            "pip",
            "compile",
            "--quiet",
            "--no-header",
            "--python-version",
            python,
            "--python-platform",
            PROBE_PLATFORM,
            str(req_file),
        ],
        min(UV_TIMEOUT_S, remaining),
    )


def _run(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(args), capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeIncomplete(f"uv did not finish within {timeout:.0f}s") from exc


def probe(
    requirements: Sequence[str],
    python: str,
    runner: Runner = _run,
    budget_s: float = PROBE_BUDGET_S,
) -> list[Blocker]:
    """Requirements that cannot be resolved for ``python``; empty means all can.

    The whole set is resolved first. Only when that fails is each requirement
    resolved on its own, so the report names the packages that block rather
    than one combined conflict. Raises ``ProbeIncomplete`` when a uv call
    times out or ``budget_s`` runs out.
    """
    deadline = time.monotonic() + budget_s
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        if _compile(requirements, python, runner, workdir, deadline).returncode == 0:
            return []
        blockers: list[Blocker] = []
        for requirement in requirements:
            result = _compile([requirement], python, runner, workdir, deadline)
            if result.returncode != 0:
                blockers.append(Blocker(requirement, _reason(result.stderr)))
        if not blockers:
            # Each resolves alone but not together: report the set's conflict.
            combined = _compile(requirements, python, runner, workdir, deadline)
            blockers.append(Blocker("(combined set)", _reason(combined.stderr)))
        return blockers


def render_report(
    window: Sequence[str], python: str, blockers: Sequence[Blocker]
) -> str:
    supported = f"{window[0]}–{window[-1]}"
    lines = [f"## Python {python} install probe", ""]
    if not blockers:
        lines.append(
            f"Every SDK dependency resolves for Python {python} on "
            f"`{PROBE_PLATFORM}`. The SDK supports {supported}; the cap can be "
            f"raised once a {python} unit leg is green."
        )
    else:
        lines.append(
            f"The SDK supports {supported}. {len(blockers)} declared "
            f"dependenc{'y' if len(blockers) == 1 else 'ies'} cannot resolve "
            f"for Python {python} on `{PROBE_PLATFORM}`:"
        )
        lines += ["", "| Requirement | Why |", "| --- | --- |"]
        for blocker in blockers:
            reason = blocker.reason.replace("|", "\\|")
            lines.append(f"| `{blocker.requirement}` | {reason} |")
    return "\n".join(lines) + "\n"


def _cmd_print_window(pyproject: Path) -> int:
    window = parse_window(requires_python(load_project(pyproject)))
    print("\n".join(window))
    print(f"next={next_version(window)}")
    return 0


def _cmd_probe_next(
    pyproject: Path, runner: Runner = _run, budget_s: float = PROBE_BUDGET_S
) -> int:
    window = parse_window(requires_python(load_project(pyproject)))
    python = next_version(window)
    try:
        blockers = probe(declared_requirements(pyproject), python, runner, budget_s)
    except OSError as exc:
        print(f"::error::could not run uv for the Python {python} probe: {exc}")
        return 2
    except ProbeIncomplete as exc:
        # Still a report, not a gate: an index outage must not fail the PR.
        append_summary(f"## Python {python} install probe\n\nIncomplete: {exc}.\n")
        print(f"::warning::Python {python} install probe incomplete: {exc}")
        return 0
    append_summary(render_report(window, python, blockers))
    if blockers:
        names = ", ".join(b.requirement for b in blockers)
        print(f"::warning::SDK cannot install on Python {python}; blocked by: {names}")
    else:
        print(f"::notice::All SDK dependencies resolve for Python {python}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The SDK's supported Python window, and a probe of the next Python."
    )
    parser.add_argument(
        "--pyproject", type=Path, default=_repo_root() / "pyproject.toml"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("print-window")
    sub.add_parser("probe-next")
    args = parser.parse_args(argv)
    if args.command == "print-window":
        return _cmd_print_window(args.pyproject)
    return _cmd_probe_next(args.pyproject)


if __name__ == "__main__":
    sys.exit(main())
