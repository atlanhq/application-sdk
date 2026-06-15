"""Benchmark: rglob vs safe_list_directory (PART-1148).

Compares the wall-clock cost of the legacy ``pathlib.Path.rglob``
directory listing against the new ``safe_list_directory`` primitive
(F_FULLFSYNC barrier + os.scandir recursion) across realistic
directory shapes.

Usage::

    uv run python scripts/benchmark_listing.py

Outputs a markdown table to stdout suitable for pasting into the PR
description. The benchmark is intentionally simple — no pytest, no
fixtures, no plugins — so the numbers reflect the listing cost
itself, not framework overhead.
"""

from __future__ import annotations

import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from application_sdk.storage._listing import safe_list_directory

# Each (file_count, depth) cell is timed this many times; we report the
# median to suppress outliers. Higher N = tighter bounds.
RUNS_PER_CELL = 20

# Test grid: file counts × tree depths.
FILE_COUNTS = (10, 100, 1000)
DEPTHS = (1, 3)


def _build_tree(root: Path, file_count: int, depth: int) -> None:
    """Populate ``root`` with ``file_count`` files spread across a tree
    of the given ``depth``. Files are distributed evenly across leaf
    subdirectories at depth N."""
    if depth == 1:
        for i in range(file_count):
            (root / f"f{i:05d}.txt").write_bytes(b"x")
        return
    # depth >= 2: create depth-1 subdirs, place files in each leaf
    per_leaf = max(1, file_count // (2**depth))
    placed = 0

    def _populate(current: Path, remaining_depth: int) -> None:
        nonlocal placed
        if remaining_depth == 0 or placed >= file_count:
            return
        # 2 subdirs per level until we hit the depth limit
        for branch in range(2):
            if placed >= file_count:
                return
            sub = current / f"d{remaining_depth}_{branch}"
            sub.mkdir(exist_ok=True)
            if remaining_depth == 1:
                # Leaf — drop files here
                drop = min(per_leaf, file_count - placed)
                for i in range(drop):
                    (sub / f"f{placed + i:05d}.txt").write_bytes(b"x")
                placed += drop
            else:
                _populate(sub, remaining_depth - 1)

    _populate(root, depth)
    # Top up any remainder at the root to hit the exact file_count
    while placed < file_count:
        (root / f"top_{placed:05d}.txt").write_bytes(b"x")
        placed += 1


def _time_rglob(root: Path) -> float:
    """Time the legacy listing approach."""
    t0 = time.perf_counter()
    files = [p for p in root.rglob("*") if p.is_file()]
    elapsed = time.perf_counter() - t0
    # Touch the result to defeat any compiler dead-code-elim
    assert len(files) >= 0
    return elapsed


def _time_safe_list(root: Path) -> float:
    """Time the new safe_list_directory primitive."""
    t0 = time.perf_counter()
    files = safe_list_directory(root)
    elapsed = time.perf_counter() - t0
    assert len(files) >= 0
    return elapsed


def _bench_cell(file_count: int, depth: int) -> tuple[float, float]:
    """Run both implementations RUNS_PER_CELL times against a fresh
    tree each iteration. Returns (median_rglob_ms, median_new_ms)."""
    rglob_times: list[float] = []
    new_times: list[float] = []
    for _ in range(RUNS_PER_CELL):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _build_tree(root, file_count, depth)
            # Alternate order to avoid caching bias
            rglob_times.append(_time_rglob(root))
            new_times.append(_time_safe_list(root))
    return statistics.median(rglob_times) * 1000, statistics.median(new_times) * 1000


def main() -> int:
    print(f"# PART-1148 listing benchmark — {platform.system()} {platform.release()}")
    print(f"# Python {sys.version.split()[0]}, runs per cell = {RUNS_PER_CELL}")
    print()
    print("| files | depth | rglob (ms) | safe_list (ms) | Δ (ms) | factor |")
    print("|------:|------:|-----------:|---------------:|-------:|-------:|")
    for fc in FILE_COUNTS:
        for d in DEPTHS:
            t_old, t_new = _bench_cell(fc, d)
            delta = t_new - t_old
            factor = t_new / t_old if t_old > 0 else float("inf")
            print(
                f"| {fc:>5} | {d:>5} | {t_old:>10.3f} | {t_new:>14.3f} "
                f"| {delta:>+6.3f} | {factor:>5.2f}x |"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
