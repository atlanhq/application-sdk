#!/usr/bin/env python3
"""Fail a PR that leaves ``packages/conformance/uv.lock`` behind the SDK (FND-4716).

The conformance lock path-sources the SDK and api packages from this checkout,
so it records their dependencies. A PR that changes those dependencies and
relocks only the root ``uv.lock`` used to merge cleanly. The drift then
surfaced in ``conformance-release.yml``, whose wheel gate refuses a stale lock,
and that failure blocked every later conformance release until someone relocked.

This runs the wheel gate's own ``lock_drift`` comparison at PR time, so the PR
fails if and only if the release gate's drift step would fail. It is not
``uv lock --check``: that also fails on every SDK or conformance version bump,
which neither release flow relocks this lock for (see conformance_wheel_gate).

Only ``tomllib`` reads, with no ``uv`` and no network, so it runs on every PR
rather than only on PRs that touch a pyproject. A drifted lock cannot reach
main past this check, so running it everywhere cannot redden unrelated PRs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conformance_wheel_gate as gate  # noqa: E402

LOCK = gate.PACKAGE_DIR / "uv.lock"
FIX = "(cd packages/conformance && uv lock)"


def main(lock_path: Path = LOCK) -> int:
    problems = gate.lock_drift(lock_path)
    if not problems:
        print(f"{lock_path.relative_to(gate.REPO_ROOT)} matches the SDK and api.")
        return 0
    for problem in problems:
        print(f"::error file=packages/conformance/uv.lock::{problem}", flush=True)
    print(
        "::error file=packages/conformance/uv.lock::packages/conformance/uv.lock "
        "does not match the dependencies the SDK and api declare, so the "
        f"conformance release would fail. Relock it and commit the result: {FIX}",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
