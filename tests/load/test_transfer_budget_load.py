"""Opt-in benchmark: four directory refs hand off within the transfer budget.

FND-4082 ``Done when``: peak RSS for 4 directory refs stays within the budget.
Before the budget, the concurrency limits multiplied — refs ×4, files ×4,
range GETs ×4 of 16 MiB — and four refs of large files added about 1 GiB.

Each ref is a directory of four files of ``ATLAN_LOAD_TEST_SIZE_MIB / 16``
(64 MiB at the default 1 GiB). The four are persisted, the local copies are
removed, and they are materialised, through the real ``persist_file_refs`` /
``materialize_file_refs`` path against a ``LocalStore``. Each phase runs in
its own process (see ``_budget_handoff_phase.py`` for why).

Peak-RSS growth of each phase must stay within the budget plus
:data:`_RUNTIME_ALLOWANCE` — memory a first transfer costs whatever the budget
is (the offload thread pool, obstore's runtime, each file's 1 MiB hash read
buffer); measured at 22–39 MiB with budgets from 32 to 512 MiB. The control
arm runs the same hand-off with a budget too large to bind and must grow past
that bound: proof the measurement can see the nesting.

Run (excluded from every default/CI suite; see tests/load/README.md):

    uv run poe load-test-storage
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import LOAD_SIZE_BYTES

pytestmark = pytest.mark.load

_MIB = 1024 * 1024
_BUDGET = 256 * _MIB
_RUNTIME_ALLOWANCE = 64 * _MIB
_FILE_BYTES = max(LOAD_SIZE_BYTES // 16, 32 * _MIB)
_PHASE_SCRIPT = Path(__file__).with_name("_budget_handoff_phase.py")


def _run_phase(phase: str, root: Path, budget: int) -> int:
    env = {**os.environ, "ATLAN_STORAGE_TRANSFER_MEMORY_BUDGET_BYTES": str(budget)}
    done = subprocess.run(
        [sys.executable, str(_PHASE_SCRIPT), phase, str(root), str(_FILE_BYTES)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(done.stdout.strip().splitlines()[-1])


def _handoff(root: Path, budget: int) -> tuple[int, int]:
    return _run_phase("persist", root, budget), _run_phase("materialize", root, budget)


def _report(label: str, persist: int, materialize: int) -> None:
    print(  # noqa: T201 — operator-facing summary; load tests run with `pytest -s`
        f"\n[load] 4 directory refs, {label}: peak RSS grew "
        f"{persist / _MIB:,.0f} MiB persisting, "
        f"{materialize / _MIB:,.0f} MiB materialising"
    )


def test_four_directory_refs_stay_within_the_budget(tmp_path: Path) -> None:
    persist, materialize = _handoff(tmp_path, _BUDGET)
    _report(f"{_BUDGET // _MIB} MiB budget", persist, materialize)
    assert persist <= _BUDGET + _RUNTIME_ALLOWANCE
    assert materialize <= _BUDGET + _RUNTIME_ALLOWANCE


def test_control_unbound_budget_grows_past_it(tmp_path: Path) -> None:
    persist, materialize = _handoff(tmp_path, 1 << 40)
    _report("no binding budget", persist, materialize)
    assert materialize > _BUDGET + _RUNTIME_ALLOWANCE
