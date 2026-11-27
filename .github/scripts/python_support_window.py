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
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

# ">=3.11,<3.15" (uv writes ">=3.11, <3.15" in the lock). Anything else is a
# shape this script does not understand, and it fails rather than guessing.
_WINDOW_RE = re.compile(r"^\s*>=\s*3\.(\d+)\s*,\s*<\s*3\.(\d+)\s*$")

# The runtime container's platform: the probe answers "would the image build".
PROBE_PLATFORM = "x86_64-manylinux_2_28"

_SELF = "atlan-application-sdk"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


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


def declared_requirements(project: Project) -> list[str]:
    """Core dependencies plus every extra's, deduplicated, in declared order.

    Self-references such as ``atlan-application-sdk[sql]`` are dropped: the
    probe resolves what the SDK pulls in, not the SDK itself, whose
    requires-python cap would refuse the target Python outright.
    """
    seen: dict[str, None] = {}
    groups = [_str_list(project.get("dependencies", []), "dependencies")]
    extras = project.get("optional-dependencies", {})
    if not isinstance(extras, dict):
        raise ValueError("optional-dependencies is not a table")
    for extra, group in cast("dict[str, object]", extras).items():
        groups.append(_str_list(group, f"optional-dependencies.{extra}"))
    for group in groups:
        for requirement in group:
            name = re.split(r"[\s\[<>=!~;(]", requirement, maxsplit=1)[0]
            if name.lower().replace("_", "-") == _SELF:
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
    requirements: Sequence[str], python: str, runner: Runner, workdir: Path
) -> subprocess.CompletedProcess[str]:
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
        ]
    )


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def probe(
    requirements: Sequence[str], python: str, runner: Runner = _run
) -> list[Blocker]:
    """Requirements that cannot be resolved for ``python``; empty means all can.

    The whole set is resolved first. Only when that fails is each requirement
    resolved on its own, so the report names the packages that block rather
    than one combined conflict.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        if _compile(requirements, python, runner, workdir).returncode == 0:
            return []
        blockers: list[Blocker] = []
        for requirement in requirements:
            result = _compile([requirement], python, runner, workdir)
            if result.returncode != 0:
                blockers.append(Blocker(requirement, _reason(result.stderr)))
        if not blockers:
            # Each resolves alone but not together: report the set's conflict.
            combined = _compile(requirements, python, runner, workdir)
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


def _cmd_probe_next(pyproject: Path, runner: Runner = _run) -> int:
    project = load_project(pyproject)
    window = parse_window(requires_python(project))
    python = next_version(window)
    try:
        blockers = probe(declared_requirements(project), python, runner)
    except OSError as exc:
        print(f"::error::could not run uv for the Python {python} probe: {exc}")
        return 2
    report = render_report(window, python, blockers)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
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
