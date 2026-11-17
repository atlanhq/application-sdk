"""The upload-time asset scan returns a report of fixed size, whatever the batch.

The scan runs in a child process and pickles its report back to the activity. A
report holding one object per failing record and one per orphan would cost memory in
proportion to the failure count three times over: in the child, in the pickle, and
again in the parent that unpickles it and walks it to build the event. These tests
pin the bound and the two promises that come with it:

* **The counts stay exact.** Only the per-failure *samples* are capped; ``total``,
  ``passed``, ``failed``, ``undeserializable``, the orphan count and the per-rule
  summary all describe the whole batch.
* **Every surface a consumer reads is unchanged.** The event map, the WARNING body
  and the shared report are compared against a golden file written by the unbounded
  walk on the same synthetic batches (``_asset_parity_support.py``).

The budget tests cover the other half: a scan that reaches its memory or wall budget
stops and reports what it read, marked as truncated, instead of being killed and
reporting nothing.
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.constants import ASSET_VALIDATION_MAX_ITEMS_PER_AXIS
from application_sdk.observability.logger_adaptor import _KNOWN_EXTRA_KEYS
from application_sdk.validation import (
    asset_validation_event_fields,
    validate_assets_as_artifact,
    validate_transformed_dir,
)
from application_sdk.validation import assets as assets_module
from application_sdk.validation.assets import (
    REFERENTIAL_CHECK_RAN,
    REFERENTIAL_CHECK_SKIPPED_BUDGET,
    TRUNCATED_RSS_BUDGET,
    TRUNCATED_WALL_BUDGET,
    AssetValidationReport,
    ScanBudget,
)
from tests.unit.validation import _asset_parity_support as parity

CAP = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS
GOLDEN = Path(__file__).parent / "resources" / "asset_validation_parity_golden.json"


def _all_failing_batch(root: Path, records: int) -> Path:
    """``records`` Columns that each fail ``.validate()`` and each name a distinct
    Table nobody emitted — every record is a failure and an orphan."""
    target = root / "transformed" / "Column" / "chunk-0.json"
    target.parent.mkdir(parents=True)
    with open(target, "wb") as handle:
        for i in range(records):
            column = parity._column(f"C{i}", f"MISSING{i}", valid=False)
            handle.write(json.dumps(column).encode() + b"\n")
    return root / "transformed"


def _summary_count(fields: dict[str, str | int], kind: str) -> int:
    rows = json.loads(str(fields["asset_validation_summary"]))
    return sum(int(row["count"]) for row in rows if row["kind"] == kind)


# ---------------------------------------------------------------------------
# Bounded samples, exact counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("records", [CAP * 4, CAP * 12])
def test_a_batch_where_every_record_fails_keeps_only_capped_samples(
    tmp_path: Path, records: int
) -> None:
    """The failing-batch shape: the samples stop at the cap, the counters do not."""
    report = validate_assets_as_artifact(_all_failing_batch(tmp_path, records))
    assets = report.assets

    assert len(assets.failures) == CAP
    assert len(assets.orphans) == CAP
    assert len(report.failures) <= 2 * CAP
    assert assets.total == records
    assert assets.passed + assets.failed == assets.total
    assert assets.failed == records
    assert assets.orphaned == records
    assert assets.referential_check == REFERENTIAL_CHECK_RAN
    assert report.failed == records

    fields = asset_validation_event_fields(assets, app_name="example-app")
    assert fields["assets_invalid"] == records
    assert fields["assets_orphaned"] == records
    # Every Column breaks the same two rules, so the per-rule rows count the
    # whole batch — read from the walk's tallies, not from the capped samples.
    assert _summary_count(fields, "invalid") == 2 * records
    assert _summary_count(fields, "orphan") == records
    assert len(json.loads(str(fields["asset_validation_matrix"]))) == 2 * CAP


def test_the_pickled_report_does_not_grow_with_the_failure_count(
    tmp_path: Path,
) -> None:
    """What crosses the process boundary is the same size at 4x and 12x the cap."""
    small = validate_assets_as_artifact(_all_failing_batch(tmp_path / "s", CAP * 4))
    large = validate_assets_as_artifact(_all_failing_batch(tmp_path / "l", CAP * 12))

    small_bytes = len(pickle.dumps(small))
    large_bytes = len(pickle.dumps(large))

    # Within 5%: only the sampled names and numbers differ in length.
    assert large_bytes < small_bytes * 1.05
    assert large_bytes < 1_000_000


def test_a_direct_call_without_a_cap_keeps_every_failure(tmp_path: Path) -> None:
    """``validate_transformed_dir`` is public, and a caller of it (the integration
    runner, the seed harness) reads the whole list; it stays unbounded by default."""
    report = validate_transformed_dir(_all_failing_batch(tmp_path, CAP * 2))

    assert len(report.failures) == CAP * 2
    assert len(report.orphans) == CAP * 2
    assert report.failed == report.orphaned == CAP * 2


def test_a_hand_built_report_still_derives_its_counts_from_its_lists() -> None:
    """A report not produced by the walk has no tallies; its lists are the record."""
    failure = assets_module.AssetValidationFailure(
        file="f.json", line=1, type_name="Table", qualified_name="q", errors=["x"]
    )
    report = AssetValidationReport(total=2, passed=1, failures=[failure])

    assert report.failed == 1
    assert report.orphaned == 0
    assert not report.ok


# ---------------------------------------------------------------------------
# Backward compatibility: every consumer surface, against the unbounded walk
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the golden holds the POSIX file paths the walk reports verbatim",
)
@pytest.mark.parametrize("batch", sorted(parity.BATCHES))
def test_every_surface_matches_the_unbounded_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: str
) -> None:
    """Byte-for-byte: the event map (matrix and summary included), the WARNING body
    at the hook's cap and at a smaller one, and the shared report's counts, body,
    matrix and event map, for the single-path, multi-part and wrapper routes.

    The golden file was written by the unbounded walk (every failure held as an
    object) over the same synthetic batches; the batches overflow the cap on both axes, sit under it,
    and are clean, so the overflow arithmetic, the failures-then-orphans ordering of
    the shared list and the clean row are all covered.
    """
    expected = json.loads(GOLDEN.read_text())[batch]
    root = tmp_path / batch
    root.mkdir()
    parity.BATCHES[batch](root)
    monkeypatch.chdir(root)

    actual = json.loads(json.dumps(parity.project_all(batch)))

    assert actual == expected


# ---------------------------------------------------------------------------
# Memory and wall budgets
# ---------------------------------------------------------------------------


EVERY = assets_module._BUDGET_CHECK_EVERY
_GIB = 1024**3


def _rss_readings(start: int, then: int):  # type: ignore[no-untyped-def]
    """A stand-in RSS reader: ``start`` for the reading taken as the scan begins,
    ``then`` for every check after it."""
    readings = iter([start])
    return lambda: next(readings, then)


def test_a_scan_past_its_rss_budget_stops_and_says_so(tmp_path: Path) -> None:
    """Past the memory budget the scan returns the records it read, no orphan
    verdict (the present-set is partial), and a truncation mark the event carries."""
    batch = _all_failing_batch(tmp_path, EVERY * 3)

    with patch.object(
        assets_module, "_current_rss_bytes", _rss_readings(_GIB // 8, 2 * _GIB)
    ):
        report = validate_assets_as_artifact(
            batch, budget=ScanBudget(max_rss_bytes=_GIB)
        )
    assets = report.assets

    assert assets.truncated == TRUNCATED_RSS_BUDGET
    assert assets.referential_check == REFERENTIAL_CHECK_SKIPPED_BUDGET
    assert assets.total == EVERY
    assert assets.failed == EVERY
    assert assets.orphaned == 0
    fields = asset_validation_event_fields(assets, app_name="example-app")
    assert fields["validation_truncated"] == TRUNCATED_RSS_BUDGET
    assert fields["assets_referential_check"] == "skipped_budget"
    assert fields["assets_total"] == EVERY
    assert "stopped at its rss budget" in assets.format_report()
    assert "stopped at its rss budget" in report.reason


def test_a_scan_under_its_rss_budget_reads_everything(tmp_path: Path) -> None:
    """Memory the process held before the scan does not count against it: a process
    already past the budget at the start still reads the whole batch while the
    scan's own growth stays under it."""
    batch = _all_failing_batch(tmp_path, EVERY * 2)

    with patch.object(
        assets_module, "_current_rss_bytes", _rss_readings(2 * _GIB, 3 * _GIB - 1)
    ):
        report = validate_assets_as_artifact(
            batch, budget=ScanBudget(max_rss_bytes=_GIB)
        )

    assert report.assets.truncated == ""
    assert report.assets.total == EVERY * 2
    assert report.assets.referential_check == REFERENTIAL_CHECK_RAN


@pytest.mark.skipif(sys.platform == "win32", reason="no RSS reading on Windows")
def test_the_rss_reading_is_this_process(tmp_path: Path) -> None:
    """A real reading, not a patched one: some MiB, and well under a TiB."""
    rss = assets_module._current_rss_bytes()

    assert rss is not None
    assert 1024**2 < rss < 1024**4


def test_a_scan_past_its_wall_budget_stops_and_says_so(tmp_path: Path) -> None:
    batch = _all_failing_batch(tmp_path, EVERY * 2)

    report = validate_assets_as_artifact(batch, budget=ScanBudget(wall_seconds=0.0))

    assert report.assets.truncated == TRUNCATED_WALL_BUDGET
    assert report.assets.total == EVERY
    assert report.assets.referential_check == REFERENTIAL_CHECK_SKIPPED_BUDGET


def test_the_orphan_pass_is_cut_by_the_wall_budget_too(tmp_path: Path) -> None:
    """Pass 1 finished, so the present-set is complete and every orphan found is
    real; the pass stops at the budget and reports what it found."""
    records = EVERY * 2
    batch = _all_failing_batch(tmp_path, records)
    # One read to set the deadline, one per check during the walk (after each
    # EVERY records, plus the first orphan-pass key), then past the deadline.
    readings = iter([0.0] * (1 + records // EVERY) + [1e9] * 100)

    with patch.object(assets_module, "_monotonic", lambda: next(readings)):
        report = validate_assets_as_artifact(
            batch, budget=ScanBudget(wall_seconds=10.0)
        )

    assets = report.assets
    assert assets.total == records
    assert assets.failed == records
    assert assets.truncated == TRUNCATED_WALL_BUDGET
    assert assets.referential_check == REFERENTIAL_CHECK_SKIPPED_BUDGET
    assert assets.orphaned == EVERY


def test_the_cell_takes_its_budgets_from_the_deployment(tmp_path: Path) -> None:
    """The upload hook reaches the cell through the wrapper, which carries no
    options, so the cell reads the two budgets itself: RSS from
    ``ATLAN_VALIDATE_ASSETS_MAX_RSS_MB``, wall from a share of the scan timeout."""
    batch = _all_failing_batch(tmp_path, EVERY * 2)

    with (
        patch("application_sdk.constants.VALIDATE_ASSETS_MAX_RSS_MB", 1),
        patch.object(
            assets_module, "_current_rss_bytes", _rss_readings(0, 2 * 1024**2)
        ),
    ):
        over_rss = validate_assets_as_artifact(batch)
    with (
        patch("application_sdk.constants.VALIDATE_ASSETS_MAX_RSS_MB", 0),
        patch.object(assets_module, "_current_rss_bytes", _rss_readings(0, _GIB)),
    ):
        rss_off = validate_assets_as_artifact(batch)
    with patch("application_sdk.constants.VALIDATE_ASSETS_TIMEOUT_SECONDS", 0.0):
        walled = validate_assets_as_artifact(batch)

    assert over_rss.assets.truncated == TRUNCATED_RSS_BUDGET
    assert rss_off.assets.truncated == ""
    assert walled.assets.truncated == TRUNCATED_WALL_BUDGET
    with patch("application_sdk.constants.VALIDATE_ASSETS_TIMEOUT_SECONDS", 600.0):
        assert ScanBudget.from_deployment().wall_seconds == pytest.approx(540.0)


def test_a_stopped_scan_that_found_nothing_is_never_clean_on_the_shared_report(
    tmp_path: Path,
) -> None:
    """The part read was fine; the rest was never checked. A generic consumer sees
    what it sees for a scan killed at the timeout — ``absent``, validator broken —
    while the asset vocabulary keeps the partial counts and the truncation mark."""
    batch = tmp_path / "transformed" / "Table" / "chunk-0.json"
    batch.parent.mkdir(parents=True)
    batch.write_text(
        "".join(json.dumps(parity._table(f"T{i}")) + "\n" for i in range(EVERY * 2))
    )

    report = validate_assets_as_artifact(
        tmp_path / "transformed",
        check_referential_integrity=False,
        budget=ScanBudget(wall_seconds=0.0),
    )

    assert report.outcome == "absent"
    assert report.validator_broken
    assert "stopped at its wall budget" in report.reason
    assert report.assets.ok
    assert report.assets.truncated == TRUNCATED_WALL_BUDGET
    assert report.assets.total == EVERY


def test_the_cell_keeps_every_failure_when_asked(tmp_path: Path) -> None:
    batch = _all_failing_batch(tmp_path, CAP * 2)

    report = validate_assets_as_artifact(batch, max_samples=None)

    assert len(report.assets.failures) == CAP * 2
    assert report.assets.failures_omitted == 0
    assert report.failure_counts is None


def test_a_sample_cap_below_one_still_lists_one_failure(tmp_path: Path) -> None:
    report = validate_transformed_dir(_all_failing_batch(tmp_path, 5), max_samples=0)

    assert len(report.failures) == 1
    assert report.failures_omitted == 4
    assert report.failed == 5
    assert "... and 4 more invalid assets" in report.format_report()


def test_an_untruncated_event_carries_no_truncation_key(tmp_path: Path) -> None:
    """The key is an addition that appears only when the scan stopped early, so the
    payload of every complete scan is exactly what it was."""
    report = validate_assets_as_artifact(_all_failing_batch(tmp_path, 3))

    fields = asset_validation_event_fields(report.assets, app_name="example-app")

    assert "validation_truncated" not in fields
    assert "validation_truncated" in _KNOWN_EXTRA_KEYS
