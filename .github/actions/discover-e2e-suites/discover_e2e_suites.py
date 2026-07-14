"""Discover a connector's e2e test files and emit a GitHub Actions matrix.

The full-DAG e2e job fans out one matrix leg per test *file* under the
connector's e2e directory (default ``tests/e2e/``), so independent suites run
as separate jobs — parallel, with live per-job logs, per-suite re-run, and
isolation. This driver globs the files and prints the two outputs the workflow
consumes:

  matrix — a JSON object ``{"include": [{"file": ..., "name": ...}, ...]}``
           suitable for ``strategy.matrix``. ``name`` is a sanitized, unique
           label used for the job name and the per-leg artifact suffix (so
           upload-artifact names don't collide across legs).
  count  — number of discovered suites (0 ⇒ the e2e job is skipped).

Co-located with the composite action — NOT under ``.github/scripts/`` — so it
is checked out alongside the action when consumed from another repo (mirrors
build_compose_chain.py). It scans the *caller's* checked-out working tree, so
the SDK never needs to know a connector's specific suites.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SANITIZE_RE = re.compile(r"[^a-z0-9]+")


def _leg_name(path: Path) -> str:
    """Derive a stable, filesystem-safe leg label from a test file path.

    ``tests/e2e/test_openapi_reuse_e2e.py`` -> ``openapi-reuse-e2e``. The
    ``test_`` prefix and ``.py`` suffix are stripped; anything else is lowercased
    and hyphenated so it is safe in a job name and an artifact suffix.
    """
    stem = path.stem
    if stem.startswith("test_"):
        stem = stem[len("test_") :]
    return _SANITIZE_RE.sub("-", stem.lower()).strip("-") or "e2e"


def discover(test_dir: str) -> list[dict[str, str]]:
    """Return the ordered matrix ``include`` entries for *test_dir*.

    One entry per ``test_*.py`` directly under *test_dir*. Sorted for a stable
    leg order. Leg names are de-duplicated defensively (two files sanitizing to
    the same label get a numeric suffix) so artifact names stay unique.
    """
    root = Path(test_dir)
    files = sorted(p for p in root.glob("test_*.py") if p.is_file())

    entries: list[dict[str, str]] = []
    seen: dict[str, int] = {}
    for path in files:
        name = _leg_name(path)
        if name in seen:
            seen[name] += 1
            name = f"{name}-{seen[name]}"
        else:
            seen[name] = 1
        entries.append({"file": path.as_posix(), "name": name})
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover e2e suites for a matrix.")
    parser.add_argument("--test-dir", default="tests/e2e")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    entries = discover(args.test_dir)
    matrix = json.dumps({"include": entries}, separators=(",", ":"))
    print(f"Discovered {len(entries)} e2e suite(s) in {args.test_dir}", file=sys.stderr)
    for e in entries:
        print(f"  - {e['name']}: {e['file']}", file=sys.stderr)
    print(f"matrix={matrix}")
    print(f"count={len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
