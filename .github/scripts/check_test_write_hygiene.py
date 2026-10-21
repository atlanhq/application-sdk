#!/usr/bin/env python3
"""Fail the unit-test leg if pytest wrote anything into the checkout (FND-4237).

Unit tests must write under ``tmp_path``. A default that resolves against the
working directory — ``./local/tmp/``, ``./components``'s object store root,
``./.hypothesis`` — puts every xdist worker on the same files at once, and the
repo's ``.gitignore`` covers all of them, so a plain ``git status --porcelain``
stays clean while it happens. This compares ``git status --porcelain
--ignored`` from before and after the run instead::

    python3 .github/scripts/check_test_write_hygiene.py snapshot "$SNAPSHOT"
    pytest ...
    python3 .github/scripts/check_test_write_hygiene.py check "$SNAPSHOT"

Untracked and ignored directories are reported collapsed (``local/``), so a
new directory is one entry and nothing inside one that already existed — the
virtualenv, say — is listed. A tracked file a test modified shows up too.

Only what pytest itself is asked to produce is allowed: the coverage reports
and bytecode caches. Anything else fails the check, naming the paths.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Callable

#: Paths the test step writes on purpose. Matched against the path git prints.
ALLOWED_PATHS = frozenset({"coverage.xml", "htmlcov/", ".coverage", ".pytest_cache/"})

Runner = Callable[[], str]


def _git_status() -> str:
    """``git status`` with ignored entries, NUL-separated so no path is quoted.

    ``check=True``: a git failure must fail the step, not read as "no changes".
    """
    return subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--ignored"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def parse_entries(status: str) -> set[str]:
    """Each ``XY path`` entry of a ``-z`` status, as one string.

    The status code is kept with the path, so a tracked file going from clean
    to modified, or an untracked one becoming ignored, counts as new.
    """
    return {entry for entry in status.split("\0") if entry}


def _path(entry: str) -> str:
    return entry[3:]


def is_allowed(entry: str) -> bool:
    path = _path(entry)
    if path in ALLOWED_PATHS or path.startswith(".coverage."):
        return True
    return path.rstrip("/").rsplit("/", 1)[-1] == "__pycache__"


def new_entries(before: set[str], after: set[str]) -> list[str]:
    """Entries present after the run, absent before, and not allowed."""
    return sorted(entry for entry in after - before if not is_allowed(entry))


def snapshot(out: Path, status: Runner = _git_status) -> int:
    out.write_text(status(), encoding="utf-8")
    return 0


def check(snapshot_file: Path, status: Runner = _git_status) -> int:
    before = parse_entries(snapshot_file.read_text(encoding="utf-8"))
    leaked = new_entries(before, parse_entries(status()))
    if not leaked:
        print("No test writes outside tmp_path.")
        return 0
    print(
        "::error::Unit tests wrote into the checkout. Root these writes under "
        "tmp_path (see local_writes_stay_out_of_the_repo in tests/unit/conftest.py):"
    )
    for entry in leaked:
        print(f"  {entry}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot").add_argument("file", type=Path)
    sub.add_parser("check").add_argument("file", type=Path)
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        return snapshot(args.file)
    return check(args.file)


if __name__ == "__main__":
    sys.exit(main())
