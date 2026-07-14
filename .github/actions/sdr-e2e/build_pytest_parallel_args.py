"""Emit pytest-xdist flags for the sdr-e2e composite action's test step.

The e2e suite runs sequentially by default. A connector opts into parallelism
by passing ``pytest-parallel-workers`` (a positive integer or ``"auto"``); this
driver validates that value and prints the two GitHub Actions outputs the test
step consumes:

  uv_with     — "" (sequential) or "--with pytest-xdist"
  xdist_args  — "" (sequential) or "-n <workers> --dist loadscope"

Moved here (rather than left as inline ``if`` shell in ``action.yaml``) per
``docs/standards/ci.md``, which reserves conditional logic in ``run:`` blocks
for tested Python. Co-located with the action — NOT under ``.github/scripts/``
— so it is checked out alongside the composite action when consumed from
another repo (mirrors ``build_compose_chain.py`` / ``select_dapr_components.py``).

Validating the value here also keeps the emitted args safe: only ``""`` / a
positive integer / ``"auto"`` are ever produced, so the downstream unquoted
shell expansion can never carry metacharacters or an invalid worker count
(e.g. ``"0"``).
"""

from __future__ import annotations

import argparse
import re
import sys

_AUTO = "auto"
# A positive integer with no leading zeros (rejects "0", "01", "-1", "").
_POSITIVE_INT_RE = re.compile(r"\A[1-9][0-9]*\Z")


def build_xdist_flags(parallel_workers: str) -> tuple[str, str]:
    """Return ``(uv_with, xdist_args)`` for a raw ``pytest-parallel-workers`` value.

    Empty / whitespace-only → sequential (both ``""``). ``"auto"`` or a positive
    integer → parallel. Any other value (``"0"``, ``"-1"``, ``"2 3"``,
    ``"auto; rm -rf /"``, …) raises :class:`ValueError` so CI fails loudly
    instead of silently mis-parallelising.
    """
    value = parallel_workers.strip()
    if not value:
        return "", ""
    if value != _AUTO and not _POSITIVE_INT_RE.match(value):
        raise ValueError(
            "pytest-parallel-workers must be a positive integer or 'auto', "
            f"got {parallel_workers!r}"
        )
    return "--with pytest-xdist", f"-n {value} --dist loadscope"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve pytest-xdist flags.")
    parser.add_argument("--parallel-workers", default="")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        uv_with, xdist_args = build_xdist_flags(args.parallel_workers)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if xdist_args:
        print(f"e2e parallelism enabled: {xdist_args}", file=sys.stderr)
    else:
        print("e2e parallelism disabled: running sequentially", file=sys.stderr)
    print(f"uv_with={uv_with}")
    print(f"xdist_args={xdist_args}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
