"""Tests for the parquet footer validator (ADR-0020, FND-689).

Three claims carry this validator, and each is asserted against the *path taken*
rather than against a convenient proxy:

* **No row is ever read.** Proved with a spy on ``pyarrow.parquet.read_schema``
  plus poisoned row-reading APIs — not by measuring how fast pyarrow happened to
  be, and not by trusting the implementation to have meant it.
* **A string-typed timestamp column is caught.** That single asymmetry — arrow
  ``timestamp[*]`` satisfies ``timestamp`` at any unit and tz, arrow ``string``
  does not — is the check that would have caught the 73-day marker-freeze RCA.
* **pyarrow's absence never breaks a hand-off.** It is an extra; the validator
  warns and skips, and the artifact still flows.

``pyarrow`` is skipped at module scope rather than per test: everything here needs
a real parquet footer, and a fixture hand-rolled to avoid the dependency would be
testing the fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

# Imports below come after pytest.importorskip() — E402 is expected.
from application_sdk.validation import parquet as parquet_module  # noqa: E402
from application_sdk.validation.artifacts import (  # noqa: E402
    FORMAT_PARQUET,
    OUTCOME_ABSENT,
    OUTCOME_CLEAN,
    OUTCOME_FLAGGED,
    OUTCOME_UNSUPPORTED,
    UNIT_COLUMN,
    ArtifactValidationReport,
    DeclaredField,
    FieldMapDeclaration,
    ModelDeclaration,
)
from application_sdk.validation.parquet import ParquetFooterValidator  # noqa: E402
from application_sdk.validation.wrapper import validate_artifact  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_parquet(path: Path, schema: "pa.Schema", rows: int = 0) -> Path:
    """Write a real parquet file carrying ``schema``.

    ``rows`` exists so a test can prove the validator ignores row groups that are
    genuinely there — a schema-only file would make "no rows read" trivially true.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = {field.name: pa.array([None] * rows, type=field.type) for field in schema}
    pq.write_table(pa.table(columns, schema=schema), path)
    return path


def _declare(*fields: DeclaredField) -> FieldMapDeclaration:
    return FieldMapDeclaration(fields=fields, artifact_format=FORMAT_PARQUET)


_HEALTHY = pa.schema(
    [
        pa.field("QUERY_ID", pa.string()),
        pa.field("START_TIME", pa.timestamp("ns", tz="UTC")),
    ]
)
"""What the consumer expects: the shape the RCA's hand-off was supposed to have."""

_DRIFTED = pa.schema(
    [
        pa.field("QUERY_ID", pa.string()),
        pa.field("START_TIME", pa.string()),
    ]
)
"""The drift itself: START_TIME stringified, everything else unchanged."""

_DECLARATION = _declare(
    DeclaredField(path="QUERY_ID", type="string"),
    DeclaredField(path="START_TIME", type="timestamp"),
)


def _validate(path: Path, declaration: FieldMapDeclaration) -> ArtifactValidationReport:
    return ParquetFooterValidator().validate(path, declaration)


# ---------------------------------------------------------------------------
# The claim the whole design rests on: no rows are read
# ---------------------------------------------------------------------------


class TestNoRowsAreRead:
    """Assert the path, not the primitive.

    "It was fast" and "pyarrow is lazy" are both true and neither is the claim.
    The claim is that this code calls exactly one metadata API, once per part, and
    never touches a row-reading one — so that is what is asserted.
    """

    _ROW_READERS = ("read_table", "read_pandas", "ParquetDataset")
    _ROW_METHODS = ("read", "read_row_group", "read_row_groups", "iter_batches")

    @staticmethod
    def _poison(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make every row-reading pyarrow API fail loudly if it is reached.

        ``ParquetFile`` itself is left alone on purpose — ``read_schema`` is built
        on it, so poisoning the constructor would forbid the very call being
        proved. Its row-reading *methods* are the honest target.
        """

        def _forbidden(name: str) -> Callable[..., None]:
            def _raise(*args: object, **kwargs: object) -> None:
                raise AssertionError(f"parquet validation read rows via {name}")

            return _raise

        for name in TestNoRowsAreRead._ROW_READERS:
            if hasattr(pq, name):
                monkeypatch.setattr(pq, name, _forbidden(f"pyarrow.parquet.{name}"))
        for name in TestNoRowsAreRead._ROW_METHODS:
            if hasattr(pq.ParquetFile, name):
                monkeypatch.setattr(
                    pq.ParquetFile, name, _forbidden(f"ParquetFile.{name}")
                )

    def test_one_footer_read_per_part_and_no_row_reader_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parts = tmp_path / "raw_queries"
        for index in range(3):
            _write_parquet(parts / f"chunk-{index}.parquet", _HEALTHY, rows=25)

        seen: list[Path] = []
        real_read_schema = pq.read_schema

        def _spy(where: Path, *args: object, **kwargs: object) -> "pa.Schema":
            seen.append(Path(where))
            return real_read_schema(where, *args, **kwargs)

        monkeypatch.setattr(pq, "read_schema", _spy)
        self._poison(monkeypatch)

        report = _validate(parts, _DECLARATION)

        # The footer API was reached exactly once per part...
        assert len(seen) == 3
        assert {p.name for p in seen} == {f"chunk-{i}.parquet" for i in range(3)}
        # ...and the poisoned row readers were never reached, which is the claim.
        assert report.outcome == OUTCOME_CLEAN
        assert report.total == 6  # 2 declared columns x 3 parts

    def test_the_spy_is_wired_to_the_call_this_module_actually_makes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard the guard: a spy on the wrong symbol would make the test above vacuous.

        ``_load_pyarrow`` resolves ``read_schema`` through the module on every call
        rather than binding it at import; if that ever changed to a cached binding,
        the monkeypatch would stop being seen and the assertion above would pass
        while proving nothing. This fails loudly instead.
        """
        _write_parquet(tmp_path / "part.parquet", _HEALTHY)

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("the spy was not the call site")

        monkeypatch.setattr(pq, "read_schema", _explode)

        # The validator never raises, so a spy that *did* take effect surfaces as
        # an `absent` report carrying the AssertionError, not as a test error.
        report = _validate(tmp_path / "part.parquet", _DECLARATION)
        assert report.outcome == OUTCOME_ABSENT
        assert "the spy was not the call site" in report.reason


# ---------------------------------------------------------------------------
# The RCA check
# ---------------------------------------------------------------------------


class TestTheStringifiedTimestamp:
    """The one distinction the capability exists to make."""

    def test_a_string_typed_timestamp_column_is_flagged_once_and_named(
        self, tmp_path: Path
    ) -> None:
        artifact = _write_parquet(tmp_path / "part.parquet", _DRIFTED, rows=10)

        report = _validate(artifact, _DECLARATION)

        assert report.outcome == OUTCOME_FLAGGED
        assert len(report.failures) == 1
        failure = report.failures[0]
        assert failure.kind == "type_mismatch"
        assert failure.field == "START_TIME"
        assert failure.expected == "timestamp"
        assert failure.actual == "string"
        assert failure.file == str(artifact)
        # QUERY_ID was still checked and still held: a drifted column does not
        # abandon the rest of the diff.
        assert report.total == 2
        assert report.passed == 1
        assert report.failed == 1

    @pytest.mark.parametrize(
        "arrow_type",
        [
            pa.timestamp("s"),
            pa.timestamp("ms"),
            pa.timestamp("us"),
            pa.timestamp("ns"),
            pa.timestamp("us", tz="UTC"),
            pa.timestamp("ns", tz="America/New_York"),
        ],
        ids=str,
    )
    def test_timestamp_holds_at_any_unit_tz_aware_or_not(
        self, tmp_path: Path, arrow_type: "pa.DataType"
    ) -> None:
        """The load-bearing row of the ADR's mapping table.

        Narrowing this to one unit, or requiring tz-naivety, would turn a correct
        hand-off into a daily false positive — and a validator people mute is worse
        than no validator.
        """
        artifact = _write_parquet(
            tmp_path / f"{arrow_type}.parquet",
            pa.schema([pa.field("START_TIME", arrow_type)]),
        )

        report = _validate(
            artifact, _declare(DeclaredField(path="START_TIME", type="timestamp"))
        )

        assert report.outcome == OUTCOME_CLEAN
        assert report.passed == 1

    def test_the_reverse_asymmetry_holds_too(self, tmp_path: Path) -> None:
        """A timestamp column does not satisfy a declared ``string``.

        Without this the mapping could be "everything satisfies everything" and the
        test above would still pass.
        """
        artifact = _write_parquet(
            tmp_path / "part.parquet",
            pa.schema([pa.field("MARKER", pa.timestamp("us"))]),
        )

        report = _validate(
            artifact, _declare(DeclaredField(path="MARKER", type="string"))
        )

        assert report.outcome == OUTCOME_FLAGGED
        assert report.failures[0].expected == "string"
        assert report.failures[0].actual.startswith("timestamp")


# ---------------------------------------------------------------------------
# The rest of the mapping table
# ---------------------------------------------------------------------------


class TestTheLogicalTypeMapping:
    """One row per ADR-0020 mapping entry, in both directions where it matters."""

    @pytest.mark.parametrize(
        ("declared", "arrow_type"),
        [
            ("string", pa.string()),
            ("string", pa.large_string()),
            ("int", pa.int8()),
            ("int", pa.int64()),
            ("int", pa.uint32()),
            ("float", pa.float32()),
            ("float", pa.float64()),
            ("decimal", pa.decimal128(38, 9)),
            ("decimal", pa.decimal256(60, 4)),
            ("bool", pa.bool_()),
            ("date", pa.date32()),
            ("date", pa.date64()),
            ("time", pa.time64("us")),
            ("binary", pa.binary()),
            ("binary", pa.large_binary()),
            ("binary", pa.binary(16)),
            # A JSON blob rides in a string column; the footer attests the carrier,
            # and whether the content parses is a row-level question this validator
            # deliberately never asks.
            ("json", pa.string()),
            ("array", pa.list_(pa.string())),
            ("struct", pa.struct([pa.field("a", pa.int64())])),
            ("map", pa.map_(pa.string(), pa.int64())),
            # `any` asserts presence only — it must accept the type it is least
            # likely to have been thinking of.
            ("any", pa.map_(pa.string(), pa.int64())),
        ],
        ids=lambda value: str(value),
    )
    def test_declared_type_is_satisfied(
        self, tmp_path: Path, declared: str, arrow_type: "pa.DataType"
    ) -> None:
        artifact = _write_parquet(
            tmp_path / f"{declared}.parquet",
            pa.schema([pa.field("COL", arrow_type)]),
        )

        report = _validate(artifact, _declare(DeclaredField(path="COL", type=declared)))

        assert report.outcome == OUTCOME_CLEAN, report.format_report()

    @pytest.mark.parametrize(
        ("declared", "arrow_type"),
        [
            ("int", pa.float64()),  # a count that became a measure
            ("float", pa.string()),  # the RCA's shape, one type over
            ("bool", pa.int8()),  # 0/1 is not a flag
            ("date", pa.timestamp("us")),  # a partition column that gained a clock
            ("decimal", pa.float64()),  # precision quietly lost
            ("struct", pa.string()),  # a payload that got serialised on the way out
            ("array", pa.string()),
            ("json", pa.struct([pa.field("a", pa.int64())])),
        ],
        ids=lambda value: str(value),
    )
    def test_declared_type_is_not_satisfied(
        self, tmp_path: Path, declared: str, arrow_type: "pa.DataType"
    ) -> None:
        artifact = _write_parquet(
            tmp_path / f"{declared}.parquet",
            pa.schema([pa.field("COL", arrow_type)]),
        )

        report = _validate(artifact, _declare(DeclaredField(path="COL", type=declared)))

        assert report.outcome == OUTCOME_FLAGGED, report.format_report()
        assert report.failures[0].kind == "type_mismatch"

    def test_a_json_pass_is_not_indistinguishable_from_a_string_pass(
        self, tmp_path: Path
    ) -> None:
        """ADR-0020: parquet ``json`` is "``string`` whose content parses as JSON".

        Only the carrier half is a metadata question. Asserting it and reporting
        `clean` with no further word would collapse `json` into `string` — the
        collapse the ADR says defeats a JSON-blob hop's check — so the unparsed half
        is named on the report. Rows are still never read to close it.
        """
        artifact = _write_parquet(
            tmp_path / "part.parquet",
            pa.schema(
                [pa.field("PAYLOAD", pa.string()), pa.field("NAME", pa.string())]
            ),
        )

        as_json = _validate(
            artifact, _declare(DeclaredField(path="PAYLOAD", type="json"))
        )
        as_string = _validate(
            artifact, _declare(DeclaredField(path="NAME", type="string"))
        )

        # Both pass on the carrier...
        assert as_json.outcome == OUTCOME_CLEAN
        assert as_string.outcome == OUTCOME_CLEAN
        # ...but only the json one says what it did not establish.
        assert "PAYLOAD" in as_json.reason
        assert "content not parsed" in as_json.reason
        assert as_string.reason == ""

    def test_a_json_carrier_that_is_not_a_string_is_still_flagged(
        self, tmp_path: Path
    ) -> None:
        """The half a footer *can* settle is still enforced, not waved through."""
        artifact = _write_parquet(
            tmp_path / "part.parquet",
            pa.schema([pa.field("PAYLOAD", pa.struct([pa.field("a", pa.int64())]))]),
        )

        report = _validate(
            artifact, _declare(DeclaredField(path="PAYLOAD", type="json"))
        )

        assert report.outcome == OUTCOME_FLAGGED
        assert report.failures[0].kind == "type_mismatch"
        # A flagged carrier is not also reported as an unestablished content check.
        assert report.reason == ""

    def test_both_partial_assertions_are_reported_together(
        self, tmp_path: Path
    ) -> None:
        """One `reason` carries every partial assertion, not just the first kind."""
        artifact = _write_parquet(
            tmp_path / "part.parquet",
            pa.schema(
                [pa.field("PAYLOAD", pa.string()), pa.field("SHAPE", pa.string())]
            ),
        )

        report = _validate(
            artifact,
            _declare(
                DeclaredField(path="PAYLOAD", type="json"),
                DeclaredField(path="SHAPE", type="geography"),  # type: ignore[arg-type]
            ),
        )

        assert report.outcome == OUTCOME_CLEAN
        assert "geography" in report.reason
        assert "PAYLOAD" in report.reason

    def test_an_unmapped_extension_type_checks_presence_and_says_so(
        self, tmp_path: Path
    ) -> None:
        """A newer toolkit's type is *our* gap, not the artifact's fault.

        Flagging it would blame the app for a mapping the SDK has not written yet;
        dropping it silently is the failure mode this whole capability exists to
        remove. So: presence asserted, type not, and the gap named on the report —
        which is what reaches the outcome event.
        """
        artifact = _write_parquet(
            tmp_path / "part.parquet", pa.schema([pa.field("COL", pa.string())])
        )

        report = _validate(
            artifact,
            _declare(DeclaredField(path="COL", type="geography")),  # type: ignore[arg-type]
        )

        assert report.outcome == OUTCOME_CLEAN
        assert "geography" in report.reason
        assert "type not asserted" in report.reason

    def test_an_unmapped_type_still_fails_when_the_column_is_absent(
        self, tmp_path: Path
    ) -> None:
        """Presence really is asserted, rather than the field being skipped whole."""
        artifact = _write_parquet(
            tmp_path / "part.parquet", pa.schema([pa.field("OTHER", pa.string())])
        )

        report = _validate(
            artifact,
            _declare(DeclaredField(path="COL", type="geography")),  # type: ignore[arg-type]
        )

        assert report.outcome == OUTCOME_FLAGGED
        assert report.failures[0].kind == "missing"


# ---------------------------------------------------------------------------
# Presence, optionality and nesting
# ---------------------------------------------------------------------------


class TestColumnResolution:
    def test_a_missing_required_column_is_flagged_and_named(
        self, tmp_path: Path
    ) -> None:
        artifact = _write_parquet(
            tmp_path / "part.parquet", pa.schema([pa.field("QUERY_ID", pa.string())])
        )

        report = _validate(artifact, _DECLARATION)

        assert report.outcome == OUTCOME_FLAGGED
        assert report.failures[0].kind == "missing"
        assert report.failures[0].field == "START_TIME"
        assert report.failures[0].expected == "timestamp"

    def test_a_missing_optional_column_is_clean(self, tmp_path: Path) -> None:
        artifact = _write_parquet(
            tmp_path / "part.parquet", pa.schema([pa.field("QUERY_ID", pa.string())])
        )

        report = _validate(
            artifact,
            _declare(
                DeclaredField(path="QUERY_ID", type="string"),
                DeclaredField(path="WAREHOUSE", type="string", required=False),
            ),
        )

        assert report.outcome == OUTCOME_CLEAN
        # The optional column still counts as a unit examined: it was looked for.
        assert report.total == 2
        assert report.passed == 2

    def test_an_optional_column_that_is_present_is_still_type_checked(
        self, tmp_path: Path
    ) -> None:
        """Optional means "may be absent", never "unchecked when present"."""
        artifact = _write_parquet(
            tmp_path / "part.parquet",
            pa.schema([pa.field("ENDED_AT", pa.string())]),
        )

        report = _validate(
            artifact,
            _declare(
                DeclaredField(path="ENDED_AT", type="timestamp", required=False),
            ),
        )

        assert report.outcome == OUTCOME_FLAGGED
        assert report.failures[0].kind == "type_mismatch"

    def test_a_dotted_path_walks_into_a_struct(self, tmp_path: Path) -> None:
        artifact = _write_parquet(
            tmp_path / "part.parquet",
            pa.schema(
                [
                    pa.field(
                        "payload",
                        pa.struct(
                            [
                                pa.field("rows", pa.list_(pa.string())),
                                pa.field("emitted_at", pa.string()),
                            ]
                        ),
                    )
                ]
            ),
        )

        report = _validate(
            artifact,
            _declare(
                DeclaredField(path="payload.rows", type="array"),
                DeclaredField(path="payload.emitted_at", type="timestamp"),
            ),
        )

        assert report.outcome == OUTCOME_FLAGGED
        assert [f.field for f in report.failures] == ["payload.emitted_at"]

    def test_a_column_literally_named_with_dots_wins_over_the_walk(
        self, tmp_path: Path
    ) -> None:
        """Flattened parquet column names contain dots; that is not a struct walk.

        Reading ``payload.rows`` as a walk when a column of that exact name exists
        would report a present column as missing — a false positive on a correct
        hand-off, which is how a validator gets muted.
        """
        artifact = _write_parquet(
            tmp_path / "part.parquet",
            pa.schema([pa.field("payload.rows", pa.list_(pa.string()))]),
        )

        report = _validate(
            artifact, _declare(DeclaredField(path="payload.rows", type="array"))
        )

        assert report.outcome == OUTCOME_CLEAN

    def test_a_dotted_path_through_a_non_struct_is_missing_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        artifact = _write_parquet(
            tmp_path / "part.parquet", pa.schema([pa.field("payload", pa.string())])
        )

        report = _validate(
            artifact, _declare(DeclaredField(path="payload.rows", type="array"))
        )

        assert report.outcome == OUTCOME_FLAGGED
        assert report.failures[0].kind == "missing"

    def test_undeclared_columns_are_not_failures(self, tmp_path: Path) -> None:
        """A declaration is a floor, not an inventory.

        A producer adding a column is a normal, compatible change; flagging it would
        make every additive release a red hand-off.
        """
        artifact = _write_parquet(
            tmp_path / "part.parquet",
            pa.schema(
                [
                    pa.field("QUERY_ID", pa.string()),
                    pa.field("START_TIME", pa.timestamp("us")),
                    pa.field("NEW_COLUMN", pa.string()),
                ]
            ),
        )

        report = _validate(artifact, _DECLARATION)

        assert report.outcome == OUTCOME_CLEAN
        assert report.total == 2  # units are *declared* columns, not footer columns


# ---------------------------------------------------------------------------
# Directories of parts
# ---------------------------------------------------------------------------


class TestPartitionedArtifacts:
    def test_drift_in_a_later_part_is_caught_and_the_part_is_named(
        self, tmp_path: Path
    ) -> None:
        """Never sampled: drift that only reached later parts is the drift worth catching."""
        parts = tmp_path / "raw_queries"
        _write_parquet(parts / "chunk-0.parquet", _HEALTHY, rows=10)
        _write_parquet(parts / "chunk-1.parquet", _HEALTHY, rows=10)
        drifted = _write_parquet(parts / "chunk-2.parquet", _DRIFTED, rows=10)

        report = _validate(parts, _DECLARATION)

        assert report.outcome == OUTCOME_FLAGGED
        assert report.total == 6
        assert report.passed == 5
        assert len(report.failures) == 1
        assert report.failures[0].file == str(drifted)

    def test_nested_partition_directories_are_walked(self, tmp_path: Path) -> None:
        parts = tmp_path / "raw_queries"
        _write_parquet(parts / "YEAR=2026" / "MONTH=08" / "p.parquet", _HEALTHY)
        _write_parquet(parts / "YEAR=2026" / "MONTH=09" / "p.parquet", _HEALTHY)

        report = _validate(parts, _DECLARATION)

        assert report.outcome == OUTCOME_CLEAN
        assert report.total == 4

    def test_a_corrupt_part_is_undecodable_and_the_others_still_get_a_verdict(
        self, tmp_path: Path
    ) -> None:
        """One bad part must not throw away every other part's answer.

        Reporting the whole artifact ``absent`` here would hide a real drift sitting
        in a readable part right next to it.
        """
        parts = tmp_path / "raw_queries"
        _write_parquet(parts / "chunk-0.parquet", _DRIFTED, rows=5)
        corrupt = parts / "chunk-1.parquet"
        corrupt.write_bytes(b"not a parquet file")

        report = _validate(parts, _DECLARATION)

        assert report.outcome == OUTCOME_FLAGGED
        assert report.total == 4  # 2 declared columns x 2 parts
        # Every declared column of the corrupt part went unchecked, and says so.
        assert report.undecodable == 2
        assert {f.file for f in report.failures if f.kind == "undecodable"} == {
            str(corrupt)
        }
        # The drift in the readable part was still found.
        assert any(f.kind == "type_mismatch" for f in report.failures)
        # The counts stay coherent: for a column scan every failure is one unit.
        assert report.failed == len(report.failures)
        assert report.passed == 1

    def test_a_directory_with_no_parts_is_absent(self, tmp_path: Path) -> None:
        empty = tmp_path / "raw_queries"
        empty.mkdir()

        report = _validate(empty, _DECLARATION)

        assert report.outcome == OUTCOME_ABSENT
        assert "no parquet file" in report.reason

    def test_a_missing_path_is_absent(self, tmp_path: Path) -> None:
        report = _validate(tmp_path / "nope", _DECLARATION)

        assert report.outcome == OUTCOME_ABSENT

    def test_a_single_unreadable_file_is_absent_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Nothing was readable, so the honest statement is about the artifact.

        Calling it ``flagged`` would blame the producer's *columns* for a file the
        validator could not open at all.
        """
        artifact = tmp_path / "part.parquet"
        artifact.write_bytes(b"not a parquet file")

        report = _validate(artifact, _DECLARATION)

        assert report.outcome == OUTCOME_ABSENT
        assert "no readable parquet footer" in report.reason
        assert report.failures == []

    def test_a_named_file_is_read_whatever_its_suffix(self, tmp_path: Path) -> None:
        """The caller said this is the artifact; suffix inference is what we avoid."""
        artifact = _write_parquet(tmp_path / "part.pq", _HEALTHY)

        report = _validate(artifact, _DECLARATION)

        assert report.outcome == OUTCOME_CLEAN

    def test_non_parquet_siblings_in_a_directory_are_ignored(
        self, tmp_path: Path
    ) -> None:
        parts = tmp_path / "raw_queries"
        _write_parquet(parts / "chunk-0.parquet", _HEALTHY)
        (parts / "_SUCCESS").write_text("")
        (parts / "manifest.json").write_text("{}")

        report = _validate(parts, _DECLARATION)

        assert report.outcome == OUTCOME_CLEAN
        assert report.total == 2


# ---------------------------------------------------------------------------
# The optional dependency
# ---------------------------------------------------------------------------


class TestPyarrowAbsent:
    """pyarrow is extra-only. Its absence is benign and must stay benign."""

    @staticmethod
    def _hide_pyarrow(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make ``import pyarrow`` raise ImportError the way a missing install does."""
        for name in list(sys.modules):
            if name == "pyarrow" or name.startswith("pyarrow."):
                monkeypatch.setitem(sys.modules, name, None)

    def test_the_loader_degrades_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The real import path is exercised, not a stubbed return value."""
        self._hide_pyarrow(monkeypatch)

        assert parquet_module._load_pyarrow() is None

    def test_it_warns_and_skips_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifact = _write_parquet(tmp_path / "part.parquet", _DRIFTED)
        self._hide_pyarrow(monkeypatch)

        with patch.object(parquet_module, "logger") as logger:
            report = _validate(artifact, _DECLARATION)

            # The warning is emitted outside the except block (a benign optional-dep
            # condition), so it carries no exc_info traceback — by design.
            logger.warning.assert_called_once()
            assert "pyarrow" in logger.warning.call_args.args[0]
            assert "exc_info" not in logger.warning.call_args.kwargs

        # Skipped, not silenced, and not a verdict against the artifact — note the
        # file above is the *drifted* one, and it is still not reported as flagged.
        assert report.outcome == OUTCOME_UNSUPPORTED
        assert report.artifact_format == FORMAT_PARQUET
        assert "pyarrow is not installed" in report.reason
        assert report.ok  # the hand-off is not failed by our own missing extra

    def test_a_missing_artifact_is_absent_even_with_no_pyarrow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whether the producer wrote anything needs no reader.

        Probing the optional dependency first would relabel "the app wrote nothing"
        as "we could not check it" on a JSON-only install — hiding a real producer
        failure behind our own missing extra, on exactly the boxes least likely to
        have it.
        """
        empty = tmp_path / "raw_queries"
        empty.mkdir()
        self._hide_pyarrow(monkeypatch)

        report = _validate(empty, _DECLARATION)

        assert report.outcome == OUTCOME_ABSENT
        assert "no parquet file" in report.reason

    def test_an_installed_but_unusable_pyarrow_degrades_and_names_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An incompatible install is a fault, not an absence, and must not raise.

        `validate()` documents that it never raises and a direct caller has no
        wrapper to catch it, so anything an arrow too old (or half-installed) throws
        on load has to land on the degrade path. Reporting it as "not installed"
        would send someone to install a package they already have.
        """
        artifact = _write_parquet(tmp_path / "part.parquet", _HEALTHY)
        # The realistic shape of the comment's "an arrow too old to expose
        # read_schema": the module imports fine, the symbol is not there.
        monkeypatch.delattr(pq, "read_schema")

        with patch.object(parquet_module, "logger") as logger:
            report = _validate(artifact, _DECLARATION)

            # A genuine fault, so unlike absence it keeps its traceback.
            logger.warning.assert_called_once()
            assert logger.warning.call_args.kwargs.get("exc_info") is True

        assert report.outcome == OUTCOME_UNSUPPORTED
        assert "installed but unusable" in report.reason
        assert "AttributeError" in report.reason
        assert report.ok  # our broken install is not a verdict on the artifact

    def test_the_wrapper_still_emits_one_outcome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: an app on a JSON-only install still gets a queryable event."""
        _write_parquet(tmp_path / "part.parquet", _HEALTHY)
        self._hide_pyarrow(monkeypatch)

        report = validate_artifact(
            tmp_path,
            _StubParquetSource(),
            validators=[ParquetFooterValidator()],
        )

        assert report.outcome == OUTCOME_UNSUPPORTED
        assert report.schema_source == "contract"
        assert report.unit == UNIT_COLUMN


# ---------------------------------------------------------------------------
# The seams: supports(), and the shape the wrapper stamps
# ---------------------------------------------------------------------------


class _StubParquetSource:
    """A schema source that answers with the standing parquet declaration."""

    def __init__(self, declaration: FieldMapDeclaration | None = None) -> None:
        self._declaration = _DECLARATION if declaration is None else declaration

    @property
    def kind(self) -> str:
        return "contract"

    def resolve(self) -> FieldMapDeclaration:
        return self._declaration


class TestTheValidatorSeam:
    def test_it_names_its_cell(self) -> None:
        validator = ParquetFooterValidator()

        assert validator.artifact_format == FORMAT_PARQUET
        assert validator.unit == UNIT_COLUMN

    def test_a_field_map_is_supported(self) -> None:
        assert ParquetFooterValidator().supports(_DECLARATION)

    def test_a_model_declaration_is_not_supported(self) -> None:
        """parquet x model: a typed model carries no column mapping to diff."""
        assert not ParquetFooterValidator().supports(
            ModelDeclaration(model=object, artifact_format=FORMAT_PARQUET)
        )

    def test_a_zero_column_declaration_is_unsupported_not_a_silent_pass(
        self, tmp_path: Path
    ) -> None:
        """A readable footer plus an empty field map must not derive ``clean``.

        The wrapper rejects this before dispatch; this asserts the direct caller —
        the path a custom source or an app-side call takes — gets the same answer
        rather than a scan over nothing that finds nothing.
        """
        artifact = _write_parquet(tmp_path / "part.parquet", _HEALTHY, rows=10)

        report = _validate(artifact, _declare())

        assert report.outcome == OUTCOME_UNSUPPORTED
        assert "zero columns" in report.reason

    def test_validating_a_model_directly_reports_unsupported_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        """A caller that skips ``supports()`` gets the same answer, not an AttributeError."""
        report = _validate(  # type: ignore[arg-type]
            tmp_path,
            ModelDeclaration(model=object, artifact_format=FORMAT_PARQUET),
        )

        assert report.outcome == OUTCOME_UNSUPPORTED
        assert "no column mapping" in report.reason

    def test_the_wrapper_stamps_the_cell_and_the_unit(self, tmp_path: Path) -> None:
        _write_parquet(tmp_path / "chunk-0.parquet", _DRIFTED, rows=3)

        report = validate_artifact(
            tmp_path,
            _StubParquetSource(),
            validators=[ParquetFooterValidator()],
            boundary=True,
        )

        assert report.outcome == OUTCOME_FLAGGED
        assert report.artifact_format == FORMAT_PARQUET
        assert report.schema_source == "contract"
        assert report.unit == UNIT_COLUMN
        assert report.fields_declared == 2
        assert report.boundary is True
        assert "START_TIME" in report.format_report()
