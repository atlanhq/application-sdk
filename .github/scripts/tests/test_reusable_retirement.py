"""A retired shim's reusable outlives the shim's last copy (FND-4654).

Bootstrap deletes a retired shim (``RETIRED_FILES``) from a consumer repo only
when that repo next runs it, so copies linger. Every lingering copy still
calls its application-sdk reusable at ``@main``; delete the reusable first and
each of those repos fails at startup on every trigger. ``auto-fix.yml`` did
exactly that: its reusable went in #4717 while 8 app clones still carried the
shim.

The order is: retire the shim, wait for C002 to report zero copies across the
fleet, then delete the reusable and record that count in
``DELETED_RETIRED_REUSABLES``. This test holds the half of that order a repo
can check on its own: a reusable a retired shim called cannot disappear from
``.github/workflows/`` without a deletion record, and every retired workflow
must say which reusable (if any) it called, so a new retirement cannot skip
the question. ``test_self_reference_pinning.py`` covers the live callers; this
covers the callers that only exist in consumer repos.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast

import pytest
import yaml
from conformance.bootstrap import render

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github/workflows"


def _is_reusable(path: Path) -> bool:
    loaded: object = yaml.safe_load(path.read_text())
    if not isinstance(loaded, dict):
        return False
    doc = cast("dict[object, object]", loaded)
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = doc.get("on", doc.get(True))
    return isinstance(triggers, dict) and "workflow_call" in triggers


def test_every_retired_workflow_is_classified() -> None:
    with_reusable = set(render.RETIRED_SHIM_REUSABLES)
    without = set(render.RETIRED_SHIMS_WITHOUT_REUSABLE)
    retired = set(render.RETIRED_WORKFLOWS)

    assert not with_reusable & without, (
        f"listed as both calling and not calling a reusable: {sorted(with_reusable & without)}"
    )
    unclassified = retired - with_reusable - without
    assert not unclassified, (
        "retired workflows must say which application-sdk reusable they called "
        "(RETIRED_SHIM_REUSABLES) or that they called none "
        f"(RETIRED_SHIMS_WITHOUT_REUSABLE): {sorted(unclassified)}"
    )
    stale = (with_reusable | without) - retired
    assert not stale, f"classified but not in RETIRED_FILES: {sorted(stale)}"


# The one deletion made before the fleet was clear. Pinned so the exception
# list cannot grow: a new deletion needs a zero count in DELETED_RETIRED_REUSABLES.
_PREMATURE_DELETIONS = {"auto-fix-vulnerabilities.yaml"}


def _record_problems(record: render.ReusableDeletion) -> list[str]:
    problems: list[str] = []
    if record.c002_copies != 0:
        problems.append(f"c002_copies is {record.c002_copies}, must be 0")
    try:
        date.fromisoformat(record.counted_on)
    except ValueError:
        problems.append(f"counted_on {record.counted_on!r} is not an ISO date")
    if not record.source.strip():
        problems.append("source is empty")
    return problems


def test_retired_shim_reusables_exist_until_recorded_deleted() -> None:
    accounted = set(render.DELETED_RETIRED_REUSABLES) | set(
        render.PREMATURE_REUSABLE_DELETIONS
    )
    missing = [
        f"{shim} -> {reusable}"
        for shim, reusable in render.RETIRED_SHIM_REUSABLES.items()
        if reusable not in accounted and not (WORKFLOWS / reusable).is_file()
    ]
    assert not missing, (
        "a reusable that a retired shim calls was deleted without a record. Fleet "
        "copies of the shim still call it @main and now fail at startup. Restore it "
        "until C002 reports zero copies, then delete it and add the count to "
        "DELETED_RETIRED_REUSABLES (docs/standards/ci.md, 'Retiring a reusable'):\n  "
        + "\n  ".join(missing)
    )


def test_kept_retired_shim_reusables_are_still_callable() -> None:
    # Present but no longer a reusable is the same startup failure.
    broken = [
        reusable
        for reusable in render.RETIRED_SHIM_REUSABLES.values()
        if (WORKFLOWS / reusable).is_file() and not _is_reusable(WORKFLOWS / reusable)
    ]
    assert not broken, f"no longer declare `on: workflow_call`: {sorted(broken)}"


def test_premature_deletions_are_closed() -> None:
    assert set(render.PREMATURE_REUSABLE_DELETIONS) == _PREMATURE_DELETIONS, (
        "PREMATURE_REUSABLE_DELETIONS is history, not an exemption list. Record a "
        "new deletion in DELETED_RETIRED_REUSABLES with a zero C002 count instead."
    )


def test_deletion_records_name_deleted_reusables() -> None:
    called = set(render.RETIRED_SHIM_REUSABLES.values())
    records = {
        **{r: None for r in render.PREMATURE_REUSABLE_DELETIONS},
        **render.DELETED_RETIRED_REUSABLES,
    }
    overlap = set(render.PREMATURE_REUSABLE_DELETIONS) & set(
        render.DELETED_RETIRED_REUSABLES
    )
    assert not overlap, f"recorded as both premature and zero-copy: {sorted(overlap)}"
    for reusable, record in records.items():
        assert reusable in called, (
            f"{reusable}: deletion record for a reusable no retired shim calls"
        )
        assert not (WORKFLOWS / reusable).exists(), (
            f"{reusable}: recorded as deleted but still present"
        )
        if record is not None:
            problems = _record_problems(record)
            assert not problems, f"{reusable}: " + "; ".join(problems)


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (render.ReusableDeletion(0, "2026-12-01", "fleet conformance ledger"), []),
        (
            render.ReusableDeletion(3, "2026-12-01", "fleet conformance ledger"),
            ["c002_copies is 3, must be 0"],
        ),
        (
            render.ReusableDeletion(0, "soon", "fleet conformance ledger"),
            ["counted_on 'soon' is not an ISO date"],
        ),
        (render.ReusableDeletion(0, "2026-12-01", "  "), ["source is empty"]),
    ],
)
def test_record_problems(record: render.ReusableDeletion, expected: list[str]) -> None:
    assert _record_problems(record) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on:\n  workflow_call:\n    inputs: {}\njobs: {}\n", True),
        ("on:\n  workflow_call:\njobs: {}\n", True),
        ("on:\n  push:\n    branches: [main]\njobs: {}\n", False),
        ("on: [push]\njobs: {}\n", False),
    ],
)
def test_is_reusable(tmp_path: Path, text: str, expected: bool) -> None:
    path = tmp_path / "wf.yaml"
    path.write_text(text)
    assert _is_reusable(path) is expected
