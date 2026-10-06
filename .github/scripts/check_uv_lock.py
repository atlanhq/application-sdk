#!/usr/bin/env python3
"""Fail a PR whose ``uv.lock`` is not the lock ``pyproject.toml`` resolves to.

Why this exists (FND-3328)
--------------------------
When ``renovate_uv_lock_bounded.py`` refuses a lock bump, it cannot stop
Renovate committing. It withholds the PR another way: it writes a lock carrying
an ``[options]`` table that ``pyproject.toml`` does not declare, which no
``--locked`` / ``--check`` consumer accepts. Until FND-3328 the consumer that
caught it was the image build's ``uv sync --locked`` behind the required
``scan / Build Image``. That job now skips on every PR except the bump-version
PR, and a skipped required check passes, so the refused lock PR would
auto-merge.

This check takes over. It runs ``uv lock --check`` in two shared workflows
every caller pins at ``@main``, so it goes live fleet-wide in the same merge as
the scan skip:

* the Pre-commit job of ``checks-reusable.yaml`` (``pre-commit / Pre-commit``,
  FND-3328), which some repos do not require or never call; and
* the Conformance Gate job of ``conformance-reusable.yaml``
  (``suite / Conformance Gate``, FND-3404), which every repo requires.

Verified on uv
0.12.23: ``uv lock --check`` exits 1 on a lock carrying the refusal's
``[options]`` table and 0 on the valid lock (red-green in
tests/test_check_uv_lock.py, which runs the real ``uv``).

When it runs
------------
Only when the lock could have changed: a repo with no ``uv.lock`` passes, and a
PR or queue entry whose ``uv.lock`` is byte-identical to its base's passes
without running ``uv``. That keeps a repo whose base already carries a stale
lock from going red on every unrelated PR; a refused lock PR always changes
``uv.lock``. Anything the comparison cannot establish (no base SHA, a failed
fetch) runs the check: the only direction an error may push is towards it.

Environment:
    BASE_SHA   the PR's / queue entry's base commit; empty outside those events.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

LOCK = "uv.lock"

Runner = Callable[[Sequence[str]], int]


def _run(cmd: Sequence[str]) -> int:
    return subprocess.run(list(cmd), check=False).returncode


def _quiet(cmd: Sequence[str]) -> int:
    return subprocess.run(
        list(cmd), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode


def lock_changed(base_sha: str, quiet: Runner = _quiet) -> bool:
    """True unless git proves ``uv.lock`` is identical to the base's."""
    if not base_sha:
        return True
    if quiet(["git", "fetch", "--no-tags", "--depth=1", "origin", base_sha]) != 0:
        return True
    # exit 0: no difference; 1: differs; anything else: unknown -> check.
    return quiet(["git", "diff", "--quiet", base_sha, "HEAD", "--", LOCK]) != 0


def main(
    root: Path = Path("."),
    env: dict[str, str] | None = None,
    run: Runner = _run,
    quiet: Runner = _quiet,
) -> int:
    env = dict(os.environ) if env is None else env
    if not (root / LOCK).is_file():
        print(f"No {LOCK}: nothing to check.", flush=True)
        return 0
    if not lock_changed(env.get("BASE_SHA", "").strip(), quiet):
        print(f"{LOCK} is unchanged from the base: not re-checked.", flush=True)
        return 0
    code = run(["uv", "lock", "--check"])
    if code != 0:
        print(
            f"::error file={LOCK}::{LOCK} is not the lock pyproject.toml resolves "
            "to (`uv lock --check` failed). If Renovate opened this PR, its "
            "bounded lock step refused the bump on purpose (see the `[options]` "
            "table's `# refusal:` comment); do not merge it as-is.",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
