r"""Build the `detect` argument list for atlan-application-sdk-conformance.

Reads EXCLUDE_PATHS, EXIT_ZERO and WITH_TESTS from the environment (set
by GitHub Actions ``env:`` blocks) and prints one argument per line to
stdout. Callers do::

    mapfile -t detect_args < <(python \
      .github/scripts/build_conformance_args.py --series C --slug ci)
    uvx atlan-application-sdk-conformance detect "${detect_args[@]}"

This keeps all conditional argument-building logic in a tested Python
script rather than inlined in YAML (per docs/standards/ci.md).

``bootstrap`` vendors this file into every consumer repo and overwrites
it on every run, so a consumer cannot fix it locally — the next
bootstrap reverts the fix. It therefore has to be lint-clean under the
strictest ruff config in the fleet, not just this repo's (FND-445):

- docstrings on the module and every public function, and a raw
  docstring here (the ``r`` prefix) because the shell example below
  contains a backslash (pydocstyle ``D``);
- every line inside 88 columns and every exploded call/signature closed
  by a magic trailing comma, so ``ruff format`` is a no-op at any
  configured line length rather than re-joining at a wider one.

``tests/test_bootstrap_scaffold_lint.py`` in the conformance package
enforces both.
"""

from __future__ import annotations

import argparse
import os
import sys


def build_args(
    series: str,
    slug: str,
    *,
    exclude: str = "",
    exit_zero: bool = False,
    with_tests: bool = False,
) -> list[str]:
    """Return the ``detect`` arguments for one conformance series.

    ``exclude`` renders as ``--exclude`` when non-empty, and
    ``exit_zero`` appends ``--exit-zero`` for soft-enforcement runs
    where violations are reported but do not fail the job.

    ``with_tests`` appends ``--with-tests``, which executes the
    registered preflight scenarios in a bounded pytest subprocess
    instead of reporting the TEST rules as not evaluated. It needs an
    importable app environment, so only pass it on a leg that also
    syncs one. The runner rejects ``--with-tests`` together with
    ``--static``, and ``--static`` is its default rather than an
    argument this script emits, so the two cannot collide here.
    """
    result = [
        "--repo",
        ".",
        "--series",
        series,
        "--output",
        f"{slug}.sarif",
    ]
    if exclude:
        result += ["--exclude", exclude]
    if exit_zero:
        result.append("--exit-zero")
    if with_tests:
        result.append("--with-tests")
    return result


def main(argv: list[str] | None = None) -> int:
    """Print one detect argument per line to stdout; return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--series",
        required=True,
        help="Conformance series letter(s)",
    )
    parser.add_argument(
        "--slug",
        required=True,
        help="Series slug for SARIF filename",
    )
    args = parser.parse_args(argv)

    exclude = os.environ.get("EXCLUDE_PATHS", "")
    exit_zero = os.environ.get("EXIT_ZERO", "").lower() == "true"
    with_tests = os.environ.get("WITH_TESTS", "").lower() == "true"

    detect_args = build_args(
        args.series,
        args.slug,
        exclude=exclude,
        exit_zero=exit_zero,
        with_tests=with_tests,
    )
    # This print is the script's actual output mechanism — the calling
    # composite action reads stdout via `mapfile`, not application
    # logging — so callers that enable ruff's T201 (no bare prints) do
    # not need a per-repo pyproject.toml ignore for this
    # bootstrap-managed file.
    print("\n".join(detect_args))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
