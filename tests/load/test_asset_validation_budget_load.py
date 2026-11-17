"""Opt-in benchmark: the upload-time asset scan stays inside its budget at scale.

The scan's child process must stay at or under 1 GiB of RSS, and the report it
pickles back must stay under 1 MB, on a batch in which every record fails and
two in five name a distinct parent the batch never emits — the shape that made
both grow with the failure count when the report held every failure as an object.
The activity side that unpickles the report and builds the event must stay flat
too. Each phase runs in its own process (see ``_asset_validation_phase.py``).

Two sizes, so the scaling is visible: the larger is
``ATLAN_LOAD_TEST_ASSET_RECORDS`` (default 1,000,000; the 5,000,000-record case
needs about 2.6 GB of disk and a few minutes), the smaller a fifth of it.

Run (excluded from every default/CI suite; see tests/load/README.md):

    uv run pytest tests/load/test_asset_validation_budget_load.py -m load -s --timeout=0
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.load

_MIB = 1024 * 1024
_RECORDS = int(os.getenv("ATLAN_LOAD_TEST_ASSET_RECORDS", "1000000"))
_CHILD_BUDGET = 1024 * _MIB
_PICKLE_BUDGET = 1_000_000
#: How far the larger run's peak may sit above the smaller's and still count as
#: flat: allocator and RocksDB memtable noise, not anything proportional to size.
_FLAT_ALLOWANCE = 128 * _MIB
_PHASE_SCRIPT = Path(__file__).with_name("_asset_validation_phase.py")


def _phase(*args: str) -> dict:
    done = subprocess.run(
        [sys.executable, str(_PHASE_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def _run(root: Path, records: int) -> tuple[dict, dict]:
    parents = records * 2 // 5
    _phase("generate", str(root), str(records), str(parents))
    scan = _phase("scan", str(root), str(root / "report.pickle"))
    project = _phase("project", str(root / "report.pickle"))
    print(  # noqa: T201 — operator-facing summary; load tests run with `pytest -s`
        f"\n[load] asset validation, {records:,} failing records / {parents:,} "
        f"missing parents: child peak {scan['peak_bytes'] / _MIB:,.0f} MiB, "
        f"report {scan['pickle_bytes']:,} B, {scan['seconds']:,.1f}s; "
        f"activity grew {project['growth_bytes'] / _MIB:,.0f} MiB"
    )
    assert scan["total"] == scan["failed"] == records
    assert scan["orphaned"] == parents
    assert scan["truncated"] == ""
    assert project["assets_invalid"] == records
    assert project["assets_orphaned"] == parents
    return scan, project


def test_the_scan_stays_inside_its_budget_and_flat(tmp_path: Path) -> None:
    small_scan, small_project = _run(tmp_path / "small", _RECORDS // 5)
    large_scan, large_project = _run(tmp_path / "large", _RECORDS)

    assert large_scan["peak_bytes"] <= _CHILD_BUDGET
    assert large_scan["pickle_bytes"] <= _PICKLE_BUDGET
    assert large_scan["peak_bytes"] <= small_scan["peak_bytes"] + _FLAT_ALLOWANCE
    assert (
        large_project["growth_bytes"] <= small_project["growth_bytes"] + _FLAT_ALLOWANCE
    )
