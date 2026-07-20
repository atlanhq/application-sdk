#!/usr/bin/env python3
"""Single source of truth for the container's Python version, plus drift checks.

The runtime container's Python is fixed by the golden base image tag in the
repo ``Dockerfile`` (``cgr.dev/.../app-framework-golden:<major.minor>``). That
tag is the *one* place the version is declared; everything else — the CI test
matrix, the integration job, and the end-to-end job — must line up with it so
what we test is what actually ships.

Three subcommands, deliberately split so the brittle part (parsing a Dockerfile)
never runs where a Dockerfile may be shaped differently:

- ``print-version``    parse the golden tag out of the Dockerfile and print the
                       ``major.minor`` (e.g. ``3.13``). SDK-repo only.
- ``check-interpreter`` compare an ``--expected major.minor`` against the
                       ``--actual`` output of a ``python --version`` invocation
                       (accepts ``Python 3.13.5`` / ``3.13.5`` / ``3.13``).
                       Exit 1 on mismatch. Generic — safe to run cross-repo
                       against any built image, since the expected value is
                       passed in rather than parsed here.
- ``check-matrix``     assert an ``--expected major.minor`` is present in a
                       ``--matrix`` list, so the container version can never
                       drift out of the versions the unit suite exercises.

Rationale for living here as a tested script rather than inline workflow shell:
the comparisons below are conditional logic, which ``docs/standards/ci.md``
requires be extracted into ``.github/scripts/`` and regression-tested (see
``tests/test_container_python_version.py``).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Matches the golden runtime base image and captures its major.minor tag.
# Anchored on the image name so an unrelated ``FROM`` (e.g. a build stage) can
# never be mistaken for the runtime base.
_GOLDEN_RE = re.compile(r"app-framework-golden:(\d+\.\d+)\b")

# Accepts "Python 3.13.5", "3.13.5", or "3.13"; captures the major.minor.
_VERSION_RE = re.compile(r"(?:Python\s+)?(\d+\.\d+)(?:\.\d+)?")


def _repo_root() -> Path:
    # <repo>/.github/scripts/container_python_version.py -> <repo>
    return Path(__file__).resolve().parents[2]


def parse_dockerfile_version(dockerfile: Path) -> str:
    """Return the golden-image ``major.minor`` declared in ``dockerfile``.

    Raises ``ValueError`` if the file has no golden base line — a loud failure
    is correct here: a Dockerfile the SoT can't read must not silently pass.
    """
    text = dockerfile.read_text(encoding="utf-8")
    matches = _GOLDEN_RE.findall(text)
    if not matches:
        raise ValueError(
            f"no 'app-framework-golden:<version>' base image found in {dockerfile}"
        )
    unique = set(matches)
    if len(unique) > 1:
        raise ValueError(
            f"conflicting golden base versions {sorted(unique)} in {dockerfile}"
        )
    return matches[0]


def normalize(version: str) -> str:
    """Reduce any accepted version spelling to ``major.minor``."""
    match = _VERSION_RE.search(version.strip())
    if not match:
        raise ValueError(f"could not parse a Python version from {version!r}")
    return match.group(1)


def _cmd_print_version(args: argparse.Namespace) -> int:
    dockerfile = (
        Path(args.dockerfile) if args.dockerfile else _repo_root() / "Dockerfile"
    )
    print(parse_dockerfile_version(dockerfile))
    return 0


def _cmd_check_interpreter(args: argparse.Namespace) -> int:
    expected = normalize(args.expected)
    actual = normalize(args.actual)
    if expected != actual:
        print(
            f"::error::container Python drift — expected {expected} (container "
            f"image), got {actual}. The tested interpreter must match the image "
            f"the code actually ships in.",
            file=sys.stderr,
        )
        return 1
    print(f"container Python matches: {actual}")
    return 0


def _cmd_check_matrix(args: argparse.Namespace) -> int:
    expected = normalize(args.expected)
    matrix = {normalize(v) for v in re.split(r"[,\s]+", args.matrix) if v.strip()}
    if expected not in matrix:
        print(
            f"::error::container Python {expected} is not in the unit test "
            f"matrix {sorted(matrix)}. Add it so the shipped interpreter is "
            f"actually exercised.",
            file=sys.stderr,
        )
        return 1
    print(f"container Python {expected} is covered by the matrix")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_print = sub.add_parser("print-version", help="print the container major.minor")
    p_print.add_argument("--dockerfile", default="", help="Dockerfile path override")
    p_print.set_defaults(func=_cmd_print_version)

    p_check = sub.add_parser("check-interpreter", help="assert actual == expected")
    p_check.add_argument("--expected", required=True)
    p_check.add_argument("--actual", required=True)
    p_check.set_defaults(func=_cmd_check_interpreter)

    p_matrix = sub.add_parser("check-matrix", help="assert expected is in matrix")
    p_matrix.add_argument("--expected", required=True)
    p_matrix.add_argument("--matrix", required=True)
    p_matrix.set_defaults(func=_cmd_check_matrix)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
