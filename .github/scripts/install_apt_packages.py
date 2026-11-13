#!/usr/bin/env python3
"""Validate a caller-supplied apt package list, then install it.

The one way a package list a caller controls reaches ``apt-get install`` in
this repo's reusable workflows. An app declares its native build headers (a
dependency with no manylinux wheel, e.g. pykerberos needing libkrb5-dev) in one
of three ways, and every one of them lands here:

- the ``apt-packages`` input of ``tests-reusable.yaml`` and
  ``conformance-reusable.yaml``, and ``apt_packages`` of
  ``build-and-publish-app.yaml`` (``--env APT_PACKAGES``);
- the ``system_deps`` input of ``checks-reusable.yaml`` (``--env SYSTEM_DEPS``);
- ``.github/ci-system-deps.txt``, written by the conformance bootstrap's
  ``--system-deps`` and installed before the conformance D series
  (``--file .github/ci-system-deps.txt``).

Every whitespace-separated token must be a plain Debian package name:
lowercase letters, digits, ``+``, ``-`` and ``.``, starting with a letter or
digit. That refuses an apt flag (``-o …``), a local package path
(``./x.deb``), a glob, a version pin (``pkg=1.2``), an arch qualifier
(``pkg:amd64``), a release (``pkg/bookworm``) and any shell syntax. A
rejected list installs nothing and the step fails. An empty list fails too:
the calling step's ``if:`` already skips when nothing is declared, so an empty
value here means the gate and the value disagree.

Packages are passed to ``apt-get`` as argv, never through a shell. Each
``apt-get`` call is bounded by ``TIMEOUT_S``: a stalled mirror or proxy fails
the step instead of holding the job until the runner's own timeout.

Usage:
    python3 install_apt_packages.py --env APT_PACKAGES
    python3 install_apt_packages.py --file .github/ci-system-deps.txt
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*")

UPDATE = ["sudo", "apt-get", "update", "-qq"]
INSTALL = ["sudo", "apt-get", "install", "-y", "--no-install-recommends"]

# Generous for a cold `apt-get update` plus a large -dev install on a hosted
# runner; far below the 6 h job ceiling a hung mirror would otherwise burn.
TIMEOUT_S = 600
# What coreutils `timeout` exits with, so the log reads as a timeout.
TIMED_OUT = 124

Runner = Callable[[Sequence[str]], int]


class InvalidPackages(ValueError):
    """The declared value is not a list of plain Debian package names."""


def parse_packages(value: str) -> list[str]:
    """Split *value* on whitespace and check every token is a package name."""
    tokens = value.split()
    if not tokens:
        raise InvalidPackages("no packages declared")
    rejected = [token for token in tokens if not PACKAGE_RE.fullmatch(token)]
    if rejected:
        raise InvalidPackages(
            "not plain Debian package names: "
            + ", ".join(repr(token) for token in rejected)
            + " (allowed: lowercase letters, digits, '+', '-', '.'; no version"
            " pins, arch qualifiers, releases, flags or paths)"
        )
    return tokens


def commands(packages: Sequence[str]) -> list[list[str]]:
    """The apt-get invocations that install *packages*, in order."""
    return [list(UPDATE), [*INSTALL, *packages]]


def _run(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(list(argv), check=False, timeout=TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        print(
            f"::error::{' '.join(argv[:3])} did not finish in {TIMEOUT_S}s",
            file=sys.stderr,
        )
        return TIMED_OUT


def _read(args: argparse.Namespace) -> tuple[str, str]:
    """Return (where the value came from, the value)."""
    if args.env is not None:
        return f"${args.env}", os.environ.get(args.env, "")
    path = Path(args.file)
    return str(path), path.read_text(encoding="utf-8")


def main(argv: Sequence[str] | None = None, run: Runner = _run) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a caller-supplied apt package list, then install it."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--env", help="environment variable holding the list")
    source.add_argument("--file", help="file holding the list")
    args = parser.parse_args(argv)

    try:
        where, value = _read(args)
    except OSError as exc:
        print(f"::error::cannot read {args.file}: {exc.strerror}", file=sys.stderr)
        return 1
    try:
        packages = parse_packages(value)
    except InvalidPackages as exc:
        print(f"::error title=apt packages rejected::{where}: {exc}", file=sys.stderr)
        return 1

    print(f"Installing apt packages from {where}: {' '.join(packages)}", flush=True)
    for command in commands(packages):
        code = run(command)
        if code != 0:
            print(f"::error::{' '.join(command[:3])} exited {code}", file=sys.stderr)
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
