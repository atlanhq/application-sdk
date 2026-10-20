#!/usr/bin/env python3
"""Import every module of a package, each in a fresh interpreter (FND-4176).

Why this exists
---------------
Conformance 0.51.0 shipped a circular import that only fired when one of a
handful of modules was the *first* thing imported. The package's own tests
import modules in an order that never hits it, so they stayed green, and the
release workflow only ran ``uv build`` — it never imported the wheel. Every
fleet repo's ``gen-contract-ledger`` then crashed on the Renovate bump.

A cycle like that is order-dependent, so no single process can rule it out:
whatever imported first decides which edge is half-initialised. This script
imports each module in its own ``python -I`` process, so every module gets to
be the entry point once. Then it runs every console script the distribution
declares with ``--help``, and every subcommand of the scripts that dispatch
through ``--commands-from``.

Console scripts are read from the installed distribution's metadata, not from
a list here, so a newly declared script is covered without touching this file
— and a wheel that lost its ``entry_points.txt`` fails for having none.

It runs in two places:

* ``packages/conformance/tests/test_import_smoke.py`` against the source tree,
  so the conformance PR checks (sdk-gate → conformance-suite-tests) catch it;
* ``conformance-release.yml`` and ``conformance-publish.yml`` against the
  built wheel in a clean venv outside the checkout. There ``--outside`` names
  the checkout, and the run fails if the package resolves from inside it:
  only an import of the installed wheel shows a file missing from the wheel.

Usage::

    import_smoke.py --python VENV/bin/python --package conformance \\
        --dist atlan-application-sdk-conformance \\
        --commands-from conformance.cli:_COMMANDS [--outside REPO]

Exit status is 1 when anything fails, else 0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

#: A module import that takes longer than this is a hang, not a slow import.
TIMEOUT_S = 120

# Resolves the package's directory without importing it: find_spec on a
# top-level name locates the package but does not execute its __init__.
_LOCATE = (
    "import importlib.util, sys\n"
    "spec = importlib.util.find_spec(sys.argv[1])\n"
    "if spec is None or not spec.submodule_search_locations:\n"
    "    sys.exit(f'{sys.argv[1]!r} is not an importable package')\n"
    "print(list(spec.submodule_search_locations)[0])\n"
)

# Console scripts of one distribution, from its installed metadata, as
# [name, "module:attr"] pairs.
_CONSOLE_SCRIPTS = (
    "import importlib.metadata as md, json, sys\n"
    "dist = md.distribution(sys.argv[1])\n"
    "print(json.dumps(sorted([ep.name, ep.value] for ep in dist.entry_points\n"
    "                        if ep.group == 'console_scripts')))\n"
)

_LIST_COMMANDS = (
    "import importlib, sys\n"
    "module, _, attr = sys.argv[1].partition(':')\n"
    "print('\\n'.join(getattr(importlib.import_module(module), attr)))\n"
)


@dataclass(frozen=True)
class Failure:
    target: str
    output: str


def _probe(python: str, code: str, arg: str) -> str:
    return subprocess.run(
        [python, "-I", "-c", code, arg],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def locate_package(python: str, package: str) -> Path:
    """Directory of *package* as *python* would import it."""
    return Path(_probe(python, _LOCATE, package).strip())


def discover_modules(package_dir: Path, package: str) -> list[str]:
    """Every importable module name under *package_dir*, sorted.

    Modules under a directory without ``__init__.py`` count: it imports as a
    namespace package (``conformance.tools`` is one). Paths with a segment that is not a
    Python identifier cannot be imported by name and are skipped.
    """
    modules: set[str] = set()
    for path in package_dir.rglob("*.py"):
        parts = list(path.relative_to(package_dir).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts and parts[-1] == "__main__":
            continue
        if all(part.isidentifier() for part in parts):
            modules.add(".".join([package, *parts]))
    return sorted(modules)


def console_scripts(python: str, dist: str) -> list[tuple[str, str]]:
    """``(name, "module:attr")`` for each console script *dist* declares."""
    return [tuple(pair) for pair in json.loads(_probe(python, _CONSOLE_SCRIPTS, dist))]


def list_commands(python: str, commands_from: str) -> list[str]:
    return [
        line
        for line in _probe(python, _LIST_COMMANDS, commands_from).splitlines()
        if line
    ]


def _tail(output: str, limit: int = 15) -> str:
    return "\n".join(output.strip().splitlines()[-limit:])


def _run(target: str, argv: list[str], cwd: Path | None = None) -> Failure | None:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, cwd=cwd, timeout=TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return Failure(target, f"timed out after {TIMEOUT_S}s")
    if result.returncode != 0:
        return Failure(target, _tail(result.stderr) or f"exit {result.returncode}")
    return None


def check_imports(python: str, modules: list[str], jobs: int) -> list[Failure]:
    def one(module: str) -> Failure | None:
        return _run(module, [python, "-I", "-c", f"import {module}"])

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return [f for f in pool.map(one, modules) if f]


def check_scripts(
    python: str, dist: str, commands_from: str | None, cwd: Path
) -> tuple[int, list[Failure]]:
    """Run each console script with ``--help``, and each subcommand of the
    scripts whose entry point lives in ``--commands-from``'s module."""
    scripts = console_scripts(python, dist)
    if not scripts:
        return 0, [Failure(dist, "declares no console_scripts entry points")]
    commands = list_commands(python, commands_from) if commands_from else []
    dispatcher = commands_from.partition(":")[0] if commands_from else None
    # The console script sits beside the interpreter in the venv's bin dir.
    bindir = Path(python).parent
    runs: list[list[str]] = []
    dispatched = False
    for name, value in scripts:
        runs.append([name, "--help"])
        if value.partition(":")[0] == dispatcher:
            dispatched = True
            runs.extend([name, command, "--help"] for command in commands)
    # Fail closed: an empty table or a dispatcher no script points at would
    # otherwise leave only the top-level --help runs, and pass.
    if commands_from is not None and not commands:
        return len(runs), [Failure(commands_from, "declares no CLI subcommands")]
    if commands_from is not None and not dispatched:
        return len(runs), [
            Failure(commands_from, f"no console script of {dist} dispatches it")
        ]
    failures = [
        f
        for argv in runs
        if (f := _run(" ".join(argv), [str(bindir / argv[0]), *argv[1:]], cwd))
    ]
    return len(runs), failures


def run(
    python: str,
    package: str,
    dist: str | None,
    commands_from: str | None,
    outside: Path | None,
    jobs: int,
) -> list[Failure]:
    package_dir = locate_package(python, package)
    print(f"{package} resolves to {package_dir}")
    if outside is not None and package_dir.resolve().is_relative_to(outside.resolve()):
        return [
            Failure(package, f"imported from inside {outside}, not from the install")
        ]
    modules = discover_modules(package_dir, package)
    if not modules:
        return [Failure(package, "no modules found")]
    failures = check_imports(python, modules, jobs)
    print(f"imported {len(modules)} modules of {package}, {len(failures)} failed")

    if dist is not None:
        # An empty cwd keeps a subcommand that writes on --help out of the
        # checkout, and keeps the checkout off a script's sys.path.
        with tempfile.TemporaryDirectory() as tmp:
            count, script_failures = check_scripts(
                python, dist, commands_from, Path(tmp)
            )
        print(
            f"ran {count} console-script invocations of {dist}, {len(script_failures)} failed"
        )
        failures.extend(script_failures)
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--python", required=True, help="interpreter to test")
    parser.add_argument("--package", required=True, help="top-level import name")
    parser.add_argument("--dist", help="distribution whose console scripts to run")
    parser.add_argument(
        "--commands-from",
        help="MODULE:ATTR whose keys are a console script's subcommands",
    )
    parser.add_argument(
        "--outside",
        type=Path,
        help="fail if the package resolves from inside this directory",
    )
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args(argv)
    if args.commands_from is not None and args.dist is None:
        parser.error("--commands-from needs --dist")

    failures = run(
        args.python,
        args.package,
        args.dist,
        args.commands_from,
        args.outside,
        args.jobs,
    )
    for failure in failures:
        print(f"\nFAIL {failure.target}\n{failure.output}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
