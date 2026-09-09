#!/usr/bin/env python3
"""Resolve the Pkl toolchain pin for CI, and check a runtime against it.

The pin's single source of truth is ``application_sdk/pkl_version.py``
(``PKL_VERSION``). This script is how *CI* reads it — textually, without
importing the SDK, because the jobs and composite actions that install pkl run
long before (or entirely without) a Python environment that has
``atlan-application-sdk`` in it, and several of them run in **consumer** repos
where the SDK repo is only present as the checked-out action.

Same shape and same reason as ``container_python_version.py``: one declared
value, everything else derives from it, and the comparisons are conditional
logic that ``docs/standards/ci.md`` requires live in a tested script rather than
inline YAML.

Subcommands:

- ``print-version``  print the pin.
- ``resolve``        print ``--requested`` when non-empty, else the pin. This is
                     what lets every workflow and action default its
                     ``pkl-version`` input to the empty string instead of
                     carrying its own copy of the literal — the copies were the
                     bug (six of them, free to drift apart).
- ``check-runtime``  compare the ``--actual`` output of a ``pkl --version``
                     invocation against the pin. Warns by default and fails with
                     ``--strict``; a mismatch is reported as *version skew*,
                     because pkl is a language and a contract that renders under
                     one release can be rejected outright by another (FND-1864).

Exit codes: 0 unless ``check-runtime --strict`` sees a mismatch (1), or the pin
cannot be read (2 — a loud failure, since silently falling back to a guessed
version is exactly the drift this file exists to remove).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Matches the SoT assignment. Anchored at column 0 on the exact constant name so
# a mention of it in a docstring or comment cannot be picked up instead.
_PIN_RE = re.compile(r'^PKL_VERSION\s*:\s*str\s*=\s*"([^"]+)"', re.MULTILINE)

# Accepts pkl's `--version` banner ("Pkl 0.32.1 (macOS 26.4, native)") or a bare
# version, with an optional pre-release suffix.
_VERSION_RE = re.compile(r"(?:Pkl\s+)?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")

SOT_RELPATH = "application_sdk/pkl_version.py"


def _repo_root() -> Path:
    # <repo>/.github/scripts/pkl_version.py -> <repo>
    return Path(__file__).resolve().parents[2]


def parse_pin(source: Path) -> str:
    """Return the ``PKL_VERSION`` literal declared in *source*.

    Raises ``ValueError`` when the assignment is absent or reshaped. Loud on
    purpose: a pin CI cannot read must fail the job, not degrade to a default
    that reintroduces the skew.
    """
    text = source.read_text(encoding="utf-8")
    matches = _PIN_RE.findall(text)
    if not matches:
        raise ValueError(
            f"no 'PKL_VERSION: str = \"<version>\"' assignment found in {source}"
        )
    unique = set(matches)
    if len(unique) > 1:
        raise ValueError(f"conflicting PKL_VERSION values {sorted(unique)} in {source}")
    return matches[0]


def read_pin(sot: Path | None = None) -> str:
    """The pin, read from the SoT (or *sot* when given)."""
    return parse_pin(sot if sot is not None else _repo_root() / SOT_RELPATH)


def normalize(version: str) -> str:
    """Reduce any accepted spelling of a pkl version to the bare version."""
    match = _VERSION_RE.search(version.strip())
    if not match:
        raise ValueError(f"could not parse a pkl version from {version!r}")
    return match.group(1)


def _cmd_print_version(args: argparse.Namespace) -> int:
    print(read_pin(Path(args.sot) if args.sot else None))
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    requested = args.requested.strip()
    if requested:
        print(requested)
        return 0
    print(read_pin(Path(args.sot) if args.sot else None))
    return 0


def _cmd_check_runtime(args: argparse.Namespace) -> int:
    # --expected overrides the pin so a caller that DELIBERATELY diverged (an
    # explicit `version:` on install-pkl) is checked against what it asked for,
    # not against the fleet pin it chose not to use. Without this the guard
    # would fail the one call site that is allowed to differ.
    expected = (
        normalize(args.expected)
        if args.expected.strip()
        else read_pin(Path(args.sot) if args.sot else None)
    )
    try:
        actual = normalize(args.actual)
    except ValueError as exc:
        # An unreadable banner is not evidence of skew — pkl may simply be
        # absent on this runner. Say so and move on unless asked to be strict.
        print(f"::warning::{exc}", file=sys.stderr)
        return 1 if args.strict else 0
    if actual != expected:
        level = "error" if args.strict else "warning"
        source = "requested" if args.expected.strip() else f"the SDK pin ({SOT_RELPATH})"
        print(
            f"::{level}::pkl version skew — this runner has {actual}, {source} is "
            f"{expected}. pkl is a language, not just a renderer: a contract that "
            f"renders under one release can be rejected outright by another, so a "
            f"render performed here does not predict the pinned one.",
            file=sys.stderr,
        )
        return 1 if args.strict else 0
    print(f"pkl {actual} matches the SDK pin.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sot",
        default="",
        help=f"override the source-of-truth path (default: <repo>/{SOT_RELPATH}).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_print = sub.add_parser("print-version", help="print the pinned pkl version")
    p_print.set_defaults(func=_cmd_print_version)

    p_resolve = sub.add_parser(
        "resolve", help="print --requested if non-empty, else the pin"
    )
    p_resolve.add_argument(
        "--requested",
        default="",
        help="a caller-supplied version; empty means 'use the pin'.",
    )
    p_resolve.set_defaults(func=_cmd_resolve)

    p_check = sub.add_parser(
        "check-runtime", help="compare a `pkl --version` output against the pin"
    )
    p_check.add_argument(
        "--actual", required=True, help="output of `pkl --version` on this runner"
    )
    p_check.add_argument(
        "--expected",
        default="",
        help="version to compare against; empty (default) means the pin.",
    )
    p_check.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 (and annotate ::error::) on skew instead of warning.",
    )
    p_check.set_defaults(func=_cmd_check_runtime)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"::error::cannot read the pkl pin: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
