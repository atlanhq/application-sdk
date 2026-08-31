"""Tests for the NDJSON declared-fields validator (ADR-0020, FND-688).

Four claims carry this validator, and each is asserted against **the real call
path** rather than against the primitive underneath it:

* **Constant memory.** Not "``iter_ndjson_lines`` is a generator" — that proves the
  iterator is lazy, not that ``validate`` consumes it lazily. The test runs the
  whole validator over a multi-file directory and watches ``tracemalloc``: peak
  allocation must stay far below the bytes on disk, which an implementation that
  materialised the artifact could not do.
* **Zero new dependencies.** Not an AST scan of the imports — that is
  ``test_artifact_dependency_floor.py``'s job and it cannot see a lazy import
  inside a function. This runs a real validation in a fresh interpreter and asserts
  no dataframe library is in ``sys.modules`` afterwards.
* **Every record is scanned.** The counts describe the whole artifact, and one
  malformed line is counted rather than allowed to abort the batch.
* **One walk in the tree.** The lifted iterator has exactly one definition, and the
  asset validator uses that one.
"""

from __future__ import annotations

import ast
import builtins
import errno
import subprocess
import sys
import tracemalloc
from pathlib import Path

import orjson
import pytest

import application_sdk.validation as validation_pkg
from application_sdk.validation.artifacts import (
    FORMAT_NDJSON,
    FORMAT_PARQUET,
    OUTCOME_ABSENT,
    OUTCOME_CLEAN,
    OUTCOME_FLAGGED,
    OUTCOME_UNSUPPORTED,
    UNIT_RECORD,
    ArtifactValidationReport,
    DeclaredField,
    FieldMapDeclaration,
    ModelDeclaration,
)
from application_sdk.validation.ndjson import NdjsonValidator, iter_ndjson_lines
from application_sdk.validation.wrapper import (
    builtin_format_validators,
    validate_artifact,
)

DECLARATION = FieldMapDeclaration(
    fields=(
        DeclaredField(path="QUERY_ID", type="string"),
        DeclaredField(path="START_TIME", type="timestamp"),
    ),
    artifact_format=FORMAT_NDJSON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, records: list[object]) -> Path:
    """Write ``records`` as NDJSON, one per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n".join(orjson.dumps(r) for r in records))
    return path


def _declare(*fields: DeclaredField) -> FieldMapDeclaration:
    return FieldMapDeclaration(fields=fields, artifact_format=FORMAT_NDJSON)


def _validate(path: Path, declaration: FieldMapDeclaration) -> ArtifactValidationReport:
    return NdjsonValidator().validate(path, declaration)


def _row(**overrides: object) -> dict:
    record = {"QUERY_ID": "q1", "START_TIME": "2026-08-25T10:11:12Z"}
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# The cell it claims, and the one it does not claim yet
# ---------------------------------------------------------------------------


def test_it_is_the_ndjson_record_validator() -> None:
    validator = NdjsonValidator()
    assert validator.artifact_format == FORMAT_NDJSON
    assert validator.unit == UNIT_RECORD


def test_it_ships_as_a_builtin() -> None:
    """An app gets NDJSON checking without naming a validator.

    Asserted as membership rather than as the whole tuple, so registering another
    format's validator (parquet, FND-689) does not read as this one regressing.
    """
    assert NdjsonValidator() in builtin_format_validators()


def test_a_delegatable_model_declaration_is_claimed() -> None:
    """Both NDJSON cells live behind this one validator (FND-690).

    Dispatch is by *format*, so a second ndjson-claiming validator would never be
    reached; the model cell is claimed here and split inside ``validate``.
    """
    from pyatlan_v9.model.assets import Asset

    assert NdjsonValidator().supports(ModelDeclaration(model=Asset)) is True


def test_an_undelegatable_model_is_unsupported_not_silently_clean() -> None:
    """The failure mode excluded is a validator that returns ``clean`` — or a whole
    artifact reported ``undecodable`` — for a model it cannot actually decode into.

    ``ModelSource`` accepts any class exposing a callable ``validate``; this cell
    decodes through pyatlan_v9's ``from_atlas_json``, so anything else gets a
    reported ``unsupported`` naming the cell.
    """
    assert NdjsonValidator().supports(ModelDeclaration(model=dict)) is False


def test_an_undelegatable_model_handed_here_directly_does_not_raise(
    tmp_path: Path,
) -> None:
    """The wrapper honours ``supports``; an app calling the validator may not."""
    report = _validate(tmp_path, ModelDeclaration(model=dict))

    assert report.outcome == OUTCOME_ABSENT
    assert "delegates to pyatlan_v9 assets" in report.reason


def test_a_non_declaration_handed_here_directly_does_not_raise(
    tmp_path: Path,
) -> None:
    """Neither a field map nor a model: still a report, never a raise."""
    report = _validate(tmp_path, object())  # type: ignore[arg-type]

    assert report.outcome == OUTCOME_ABSENT
    assert "not a field map or a model" in report.reason


# ---------------------------------------------------------------------------
# The per-record check
# ---------------------------------------------------------------------------


def test_a_conforming_artifact_is_clean(tmp_path: Path) -> None:
    _write(tmp_path / "part-0.json", [_row(), _row(QUERY_ID="q2")])

    report = _validate(tmp_path, DECLARATION)

    assert report.outcome == OUTCOME_CLEAN
    assert (report.total, report.passed, report.failed) == (2, 2, 0)


def test_a_stringified_timestamp_is_flagged(tmp_path: Path) -> None:
    """The distinction the whole capability exists to make.

    A production RCA traced a 73-day frozen lineage marker to one column that had
    become a string where the consumer expected a timestamp, with every workflow in
    the chain reporting success throughout. In NDJSON a timestamp is *always*
    carried by a string, so the check that reproduces that catch is whether the
    string is ISO-8601 at all rather than free-form text.
    """
    _write(
        tmp_path / "part-0.json",
        [_row(), _row(START_TIME="last tuesday"), _row(START_TIME="")],
    )

    report = _validate(tmp_path, DECLARATION)

    assert report.outcome == OUTCOME_FLAGGED
    assert (report.total, report.passed) == (3, 1)
    kinds = {f.kind for f in report.failures}
    assert kinds == {"type_mismatch"}
    flagged = report.failures[0]
    assert (flagged.field, flagged.expected, flagged.actual) == (
        "START_TIME",
        "timestamp",
        "string",
    )
    assert flagged.line == 2


def test_an_epoch_number_satisfies_timestamp(tmp_path: Path) -> None:
    """Both carriers the ADR names, and no unit guessing between them."""
    _write(
        tmp_path / "part-0.json",
        [_row(START_TIME=1_756_108_272), _row(START_TIME=1_756_108_272_000.5)],
    )

    assert _validate(tmp_path, DECLARATION).outcome == OUTCOME_CLEAN


@pytest.mark.parametrize(
    "date_only", ["2026-08-25", "20260825", "2026-W35-2"], ids=lambda s: s
)
def test_a_date_only_string_does_not_satisfy_timestamp(
    tmp_path: Path, date_only: str
) -> None:
    """``date`` and ``timestamp`` are separate rows in the ADR's mapping.

    ``datetime.fromisoformat`` happily reads a date-only string as midnight, so
    without a separator gate this would pass — while
    ``test_the_logical_type_mapping`` already asserts the reverse, that a declared
    ``date`` rejects a full datetime. A one-directional acceptance is how two
    logical types quietly collapse into one.
    """
    _write(tmp_path / "part-0.json", [_row(START_TIME=date_only)])

    report = _validate(tmp_path, DECLARATION)

    assert report.outcome == OUTCOME_FLAGGED
    assert report.failures[0].kind == "type_mismatch"
    assert report.failures[0].field == "START_TIME"


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-08-25T10:11:12",
        "2026-08-25t10:11:12",
        "2026-08-25 10:11:12",
        "20260825T101112",
        "2026-08-25T10:11:12Z",
        "2026-08-25T10:11:12.123456+05:30",
    ],
    ids=lambda s: s,
)
def test_the_separator_gate_costs_no_real_timestamp(tmp_path: Path, stamp: str) -> None:
    """Every date-time form the stdlib reads carries a ``T``, ``t`` or space.

    Asserted across the separators, the basic form and both tz spellings, because
    the gate is a *pre*-parse rejection: if any accepted form lacked a separator,
    the gate would silently narrow what counts as a timestamp.
    """
    _write(tmp_path / "part-0.json", [_row(START_TIME=stamp)])

    assert _validate(tmp_path, DECLARATION).outcome == OUTCOME_CLEAN


def test_a_missing_required_field_is_reported_per_record(tmp_path: Path) -> None:
    _write(tmp_path / "part-0.json", [_row(), {"QUERY_ID": "q2"}])

    report = _validate(tmp_path, DECLARATION)

    assert report.outcome == OUTCOME_FLAGGED
    assert (report.total, report.passed) == (2, 1)
    assert report.failures[0].kind == "missing"
    assert report.failures[0].field == "START_TIME"


def test_an_optional_field_is_type_checked_only_when_present(tmp_path: Path) -> None:
    declaration = _declare(
        DeclaredField(path="QUERY_ID", type="string"),
        DeclaredField(path="ROWS", type="int", required=False),
    )
    _write(
        tmp_path / "part-0.json",
        [{"QUERY_ID": "q1"}, {"QUERY_ID": "q2", "ROWS": 5}],
    )

    assert _validate(tmp_path, declaration).outcome == OUTCOME_CLEAN

    _write(tmp_path / "part-0.json", [{"QUERY_ID": "q1", "ROWS": "five"}])
    report = _validate(tmp_path, declaration)
    assert report.outcome == OUTCOME_FLAGGED
    assert report.failures[0].kind == "type_mismatch"


def test_one_record_can_break_on_several_fields(tmp_path: Path) -> None:
    """``failed`` counts records; ``len(failures)`` counts problems."""
    _write(tmp_path / "part-0.json", [{"QUERY_ID": 1, "START_TIME": "nope"}])

    report = _validate(tmp_path, DECLARATION)

    assert (report.total, report.passed, report.failed) == (1, 0, 1)
    assert len(report.failures) == 2


def test_a_json_null_satisfies_any_declared_type(tmp_path: Path) -> None:
    """Deliberate, and the reasoning is not "nulls are fine".

    The vocabulary has no nullability axis, and the parquet validator diffs a
    *footer schema*, which cannot see nulls at all — an arrow ``timestamp`` column
    is a timestamp column whether or not every value in it is null. Flagging nulls
    here would make the two formats give different verdicts on the same
    declaration.
    """
    _write(tmp_path / "part-0.json", [{"QUERY_ID": None, "START_TIME": None}])

    report = _validate(tmp_path, DECLARATION)

    assert report.outcome == OUTCOME_CLEAN
    assert report.passed == 1


def test_a_bool_does_not_satisfy_int(tmp_path: Path) -> None:
    """Python makes ``bool`` a subclass of ``int``; JSON does not."""
    declaration = _declare(DeclaredField(path="ROWS", type="int"))
    _write(tmp_path / "part-0.json", [{"ROWS": True}])

    report = _validate(tmp_path, declaration)

    assert report.outcome == OUTCOME_FLAGGED
    assert report.failures[0].actual == "bool"


@pytest.mark.parametrize(
    ("declared_type", "accepted", "rejected"),
    [
        ("string", "abc", 1),
        ("int", 7, 7.5),
        ("float", 7, "7"),
        ("bool", False, 0),
        ("timestamp", "2026-08-25T10:11:12+05:30", "yesterday"),
        ("date", "2026-08-25", "2026-08-25T10:11:12"),
        ("time", "10:11:12", "2026-08-25"),
        ("decimal", "1234.5678", "not a number"),
        ("binary", "aGVsbG8=", "not*base64"),
        ("json", {"a": 1}, "plain text"),
        ("array", [1, 2], {"a": 1}),
        ("struct", {"a": 1}, [1, 2]),
        ("map", {"a": 1}, "a=1"),
        ("any", "anything at all", None),
    ],
)
def test_the_logical_type_mapping(
    tmp_path: Path, declared_type: str, accepted: object, rejected: object
) -> None:
    """The ADR's NDJSON column, asserted row by row.

    ``any`` has no rejected value by construction — that is what it means — so it
    is parametrized with ``None``, which passes for every type anyway.
    """
    declaration = _declare(DeclaredField(path="F", type=declared_type))  # type: ignore[arg-type]

    _write(tmp_path / "part-0.json", [{"F": accepted}])
    assert _validate(tmp_path, declaration).outcome == OUTCOME_CLEAN

    _write(tmp_path / "part-0.json", [{"F": rejected}])
    expected = OUTCOME_CLEAN if declared_type == "any" else OUTCOME_FLAGGED
    assert _validate(tmp_path, declaration).outcome == expected


def test_json_accepts_a_blob_carried_in_a_string(tmp_path: Path) -> None:
    """The hop this member exists for: physically a string, semantically JSON."""
    declaration = _declare(DeclaredField(path="F", type="json"))
    _write(tmp_path / "part-0.json", [{"F": '{"a": 1}'}, {"F": [1, 2]}])

    assert _validate(tmp_path, declaration).outcome == OUTCOME_CLEAN


# ---------------------------------------------------------------------------
# Dotted paths
# ---------------------------------------------------------------------------


def test_a_dotted_path_addresses_a_nested_field(tmp_path: Path) -> None:
    declaration = _declare(DeclaredField(path="payload.rows", type="array"))
    _write(tmp_path / "part-0.json", [{"payload": {"rows": [1, 2]}}])

    assert _validate(tmp_path, declaration).outcome == OUTCOME_CLEAN


def test_a_dotted_path_through_a_non_object_does_not_resolve(tmp_path: Path) -> None:
    """ "``payload`` is a string" is a more useful report than "``rows`` is absent"."""
    declaration = _declare(DeclaredField(path="payload.rows", type="array"))
    _write(tmp_path / "part-0.json", [{"payload": "oops"}])

    report = _validate(tmp_path, declaration)

    assert report.outcome == OUTCOME_FLAGGED
    assert report.failures[0].kind == "missing"
    assert "payload is string, not an object" in report.failures[0].errors[0]


def test_a_dotted_path_names_the_segment_that_was_absent(tmp_path: Path) -> None:
    declaration = _declare(DeclaredField(path="payload.rows", type="array"))
    _write(tmp_path / "part-0.json", [{"payload": {"cols": []}}])

    report = _validate(tmp_path, declaration)

    assert "'payload.rows' is absent" in report.failures[0].errors[0]


# ---------------------------------------------------------------------------
# Undecodable records, and never aborting the batch
# ---------------------------------------------------------------------------


def test_a_broken_line_is_counted_not_raised(tmp_path: Path) -> None:
    """Same posture as the asset path: one bad line must not take the batch."""
    path = tmp_path / "part-0.json"
    path.write_bytes(
        b"\n".join([orjson.dumps(_row()), b"{not json", orjson.dumps(_row())])
    )

    report = _validate(tmp_path, DECLARATION)

    assert (report.total, report.passed, report.undecodable) == (3, 2, 1)
    assert report.failures[0].line == 2


def test_a_record_that_is_not_an_object_is_undecodable(tmp_path: Path) -> None:
    """Not one ``missing`` per declared field: it has no addressable fields at all."""
    _write(tmp_path / "part-0.json", [[1, 2], "a string", _row()])

    report = _validate(tmp_path, DECLARATION)

    assert (report.total, report.passed, report.undecodable) == (3, 1, 2)
    assert {f.kind for f in report.failures} == {"undecodable"}
    assert "carries no addressable fields" in report.failures[0].errors[0]


def test_blank_lines_are_not_records(tmp_path: Path) -> None:
    path = tmp_path / "part-0.json"
    path.write_bytes(orjson.dumps(_row()) + b"\n\n   \n" + orjson.dumps(_row()) + b"\n")

    report = _validate(tmp_path, DECLARATION)

    assert (report.total, report.passed) == (2, 2)


# ---------------------------------------------------------------------------
# Nothing to read is never a pass
# ---------------------------------------------------------------------------


def test_a_missing_path_is_absent(tmp_path: Path) -> None:
    report = _validate(tmp_path / "nope", DECLARATION)

    assert report.outcome == OUTCOME_ABSENT
    assert report.total == 0


def test_an_empty_directory_is_absent_not_clean(tmp_path: Path) -> None:
    """Zero records checked must never show up as a pass on the board.

    This is the exact failure ADR-0020 was written against: the earlier
    upload-time hook could validate nothing and look adopted.
    """
    report = _validate(tmp_path, DECLARATION)

    assert report.outcome == OUTCOME_ABSENT
    assert "no ndjson records" in report.reason


def test_an_unreadable_file_is_absent_not_a_partial_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure is injected at the opener, deliberately not via ``chmod``.

    ``chmod(0o000)`` does not revoke read on Windows, and Windows is in the unit
    matrix — so that version of this test would let the scan succeed on one leg
    while still reporting green, which is the contract going untested rather than
    tested. Refusing the ``open`` call behaves identically on every platform.

    The second file is the one that fails, so the assertion that matters is
    ``total == 0``: what was counted before the failure describes a partial scan
    nobody should act on, and it is discarded rather than reported.
    """
    _write(tmp_path / "part-0.json", [_row()])
    blocked = _write(tmp_path / "part-1.json", [_row()])
    real_open = builtins.open

    def refusing_open(file: object, *args: object, **kwargs: object) -> object:
        if isinstance(file, (str, Path)) and Path(file) == blocked:
            raise PermissionError(errno.EACCES, "Permission denied", str(blocked))
        return real_open(file, *args, **kwargs)  # type: ignore[arg-type,call-overload]

    monkeypatch.setattr(builtins, "open", refusing_open)

    report = _validate(tmp_path, DECLARATION)

    assert report.outcome == OUTCOME_ABSENT
    assert report.total == 0


# ---------------------------------------------------------------------------
# A declared type this validator cannot map
# ---------------------------------------------------------------------------


def test_an_unmappable_type_degrades_to_presence_only(tmp_path: Path) -> None:
    """A newer toolkit's type must not invalidate every other assertion.

    The loader deliberately does not police the type vocabulary. Dropping the field
    here would undo that one layer down, and failing the file on it would undo it
    completely — so presence is still asserted, the type is not, and the report
    says which fields that happened to.
    """
    declaration = _declare(
        DeclaredField(path="QUERY_ID", type="string"),
        DeclaredField(path="EXOTIC", type="interval"),  # type: ignore[arg-type]
    )
    _write(tmp_path / "part-0.json", [{"QUERY_ID": "q1", "EXOTIC": 12345}])

    report = _validate(tmp_path, declaration)

    assert report.outcome == OUTCOME_CLEAN
    assert "EXOTIC:interval" in report.reason

    # Presence is still asserted for it, and the other field still type-checks.
    _write(tmp_path / "part-0.json", [{"QUERY_ID": 1}])
    report = _validate(tmp_path, declaration)
    assert {f.kind for f in report.failures} == {"missing", "type_mismatch"}


# ---------------------------------------------------------------------------
# Through the public wrapper
# ---------------------------------------------------------------------------


class _StubSource:
    """A schema source whose answer the test dictates."""

    def __init__(self, answer: object) -> None:
        self._answer = answer

    @property
    def kind(self) -> str:
        return "contract"

    def resolve(self) -> object:
        return self._answer


def test_the_wrapper_stamps_the_cell_onto_a_real_scan(tmp_path: Path) -> None:
    _write(tmp_path / "part-0.json", [_row(), _row(START_TIME="whenever")])

    report = validate_artifact(tmp_path, _StubSource(DECLARATION), boundary=True)

    assert report.outcome == OUTCOME_FLAGGED
    assert report.artifact_format == FORMAT_NDJSON
    assert report.schema_source == "contract"
    assert report.unit == UNIT_RECORD
    assert report.fields_declared == 2
    assert report.boundary is True


def test_a_parquet_declaration_is_never_claimed_by_this_validator(
    tmp_path: Path,
) -> None:
    """Dispatch is on the declared format, and NDJSON never answers for parquet.

    FND-689 registered a parquet validator alongside this one, so a parquet
    declaration now reaches a footer diff rather than "no validator registered".
    What must not change is that *this* validator never claims it: dispatching a
    parquet file to a line-by-line scan would read its binary as records and report
    a whole artifact of undecodable ones — a loud wrong answer in place of a
    correct check.

    The validator set is pinned rather than left to the builtins, so this stays a
    statement about NDJSON instead of about whichever formats happen to ship.
    """
    assert NdjsonValidator().artifact_format != FORMAT_PARQUET

    declaration = FieldMapDeclaration(
        fields=(DeclaredField(path="QUERY_ID", type="string"),),
        artifact_format=FORMAT_PARQUET,
    )

    report = validate_artifact(
        tmp_path, _StubSource(declaration), validators=[NdjsonValidator()]
    )

    assert report.outcome == OUTCOME_UNSUPPORTED
    assert "no validator registered" in report.reason


# ---------------------------------------------------------------------------
# Assert the path, not the primitive: memory
# ---------------------------------------------------------------------------


def _fat_directory(root: Path, *, files: int, per_file: int) -> int:
    """Write a multi-file NDJSON directory; return its total size in bytes."""
    filler = "x" * 160
    for n in range(files):
        _write(
            root / f"part-{n}.json",
            [_row(QUERY_ID=f"q{n}-{i}", NOTE=filler) for i in range(per_file)],
        )
    return sum(p.stat().st_size for p in root.rglob("*.json"))


def test_memory_does_not_scale_with_the_artifact(tmp_path: Path) -> None:
    """Constant memory, asserted of ``validate`` — not of the iterator.

    A generator that nobody drains lazily is still a generator, so proving
    ``iter_ndjson_lines`` yields proves nothing about the validator. This runs the
    real scan over a multi-file directory and asserts two things an implementation
    that materialised the artifact could not satisfy: peak allocation stays a small
    fraction of the bytes on disk, and growing the artifact 100x does not grow the
    peak.
    """
    small = tmp_path / "small"
    large = tmp_path / "large"
    _fat_directory(small, files=2, per_file=100)
    large_bytes = _fat_directory(large, files=20, per_file=1000)

    peaks: dict[str, int] = {}
    for name, root in (("small", small), ("large", large)):
        tracemalloc.start()
        tracemalloc.reset_peak()
        report = _validate(root, DECLARATION)
        peaks[name] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert report.outcome == OUTCOME_CLEAN

    assert large_bytes > 4_000_000, "the large artifact must dwarf the peak to matter"
    assert peaks["large"] < large_bytes // 10, (
        f"peak {peaks['large']} is not a small fraction of {large_bytes} bytes on "
        f"disk — the scan is holding the artifact rather than streaming it"
    )
    assert peaks["large"] < peaks["small"] * 3, (
        f"100x the records took {peaks['large'] / max(peaks['small'], 1):.1f}x the "
        f"peak memory — the scan is not constant-memory"
    )


def test_every_record_is_scanned_never_sampled(tmp_path: Path) -> None:
    """The counts describe the whole artifact, across every file in the directory."""
    _fat_directory(tmp_path, files=5, per_file=400)

    report = _validate(tmp_path, DECLARATION)

    assert (report.total, report.passed) == (2000, 2000)


# ---------------------------------------------------------------------------
# Assert the path, not the primitive: the dependency floor
# ---------------------------------------------------------------------------


_JSON_ONLY_CALLER = """
import sys, pathlib, orjson
from application_sdk.validation import validate_artifact
from application_sdk.validation.artifacts import (
    FORMAT_NDJSON, DeclaredField, FieldMapDeclaration,
)

class Source:
    kind = "contract"
    def resolve(self):
        return FieldMapDeclaration(
            fields=(DeclaredField(path="QUERY_ID", type="string"),),
            artifact_format=FORMAT_NDJSON,
        )

target = pathlib.Path(sys.argv[1])
report = validate_artifact(target, Source())
assert report.outcome == "clean", report.outcome
print(",".join(m for m in ("pyarrow", "pandas", "pandera") if m in sys.modules))
"""


def test_a_json_only_caller_never_loads_a_dataframe_library(tmp_path: Path) -> None:
    """Zero new dependencies, asserted of the running process.

    ``test_artifact_dependency_floor.py`` reads the imports statically, which is the
    right check for a module-level import but cannot see a lazy one inside a
    function — and the parquet validator's ``pyarrow`` import will be exactly that.
    So this one runs a real end-to-end validation in a fresh interpreter and asks
    the process itself what got loaded.
    """
    _write(tmp_path / "part-0.json", [{"QUERY_ID": "q1"}])
    script = tmp_path / "json_only_caller.py"
    script.write_text(_JSON_ONLY_CALLER)

    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    loaded = result.stdout.strip()
    assert not loaded, f"a JSON-only validation loaded {loaded} — see ADR-0020"


# ---------------------------------------------------------------------------
# One walk in the tree
# ---------------------------------------------------------------------------


def test_the_ndjson_walk_is_defined_exactly_once() -> None:
    """The iterator was lifted, not copied.

    A second walk would be a second set of decisions about blank lines, file
    ordering and directory recursion to keep in sync — which is how two callers
    start disagreeing about what a record is.
    """
    package_root = Path(validation_pkg.__file__).parent
    definitions = [
        module
        for module in sorted(package_root.rglob("*.py"))
        for node in ast.walk(ast.parse(module.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name.endswith("iter_ndjson_lines")
    ]

    assert definitions == [package_root / "ndjson.py"]


def test_the_asset_validator_uses_the_lifted_walk() -> None:
    from application_sdk.validation import assets

    assert assets.iter_ndjson_lines is iter_ndjson_lines


def test_the_lifted_walk_still_yields_file_line_and_bytes(tmp_path: Path) -> None:
    """Lifted verbatim: same tuple, same 1-based lines, same blank-line skipping."""
    path = tmp_path / "part-0.json"
    path.write_bytes(b'{"a": 1}\n\n{"a": 2}\n')

    assert list(iter_ndjson_lines(path)) == [
        (str(path), 1, b'{"a": 1}'),
        (str(path), 3, b'{"a": 2}'),
    ]
    assert list(iter_ndjson_lines(tmp_path / "nope")) == []


# ---------------------------------------------------------------------------
# Both walk branches require .json (FND-802 cause 4)
# ---------------------------------------------------------------------------
#
# The single-file branch used to accept ANY file while the directory branch
# globbed `*.json`, so the same subtree validated differently depending on
# whether the caller named the directory or one file inside it.
#
# In production that asymmetry manufactured findings. `_resolve_transformed_target`
# returns any single file whose path contains `transformed`, so a connector
# calling `upload(".../transformed/transformed-count.txt")` handed the walk a
# sidecar holding a bare record count. It was read as NDJSON, the integer failed
# to decode as an asset, and the run reported one phantom `undeserializable`
# every single run — for a file the directory walk had always ignored.


def test_single_file_branch_skips_a_non_json_file(tmp_path: Path) -> None:
    sidecar = tmp_path / "transformed-count.txt"
    sidecar.write_bytes(b"194045")

    assert list(iter_ndjson_lines(sidecar)) == []


def test_the_count_sidecar_yields_nothing_rather_than_a_phantom_record(
    tmp_path: Path,
) -> None:
    """The exact production shape. A bare integer must not become a record —
    "nothing to validate" is the honest answer for a path holding no asset
    parts, and strictly better than inventing a failure."""
    transformed = tmp_path / "transformed"
    transformed.mkdir()
    (transformed / "transformed-count.txt").write_bytes(b"194045")

    assert list(iter_ndjson_lines(transformed / "transformed-count.txt")) == []


def test_the_two_branches_agree_on_the_same_subtree(tmp_path: Path) -> None:
    """The invariant the fix restores: naming the directory and naming the part
    inside it must see the same records, and neither must see the sidecar."""
    transformed = tmp_path / "transformed"
    transformed.mkdir()
    part = transformed / "entities.json"
    part.write_bytes(b'{"a": 1}\n{"a": 2}\n')
    (transformed / "transformed-count.txt").write_bytes(b"2")

    via_dir = [(line, raw) for _, line, raw in iter_ndjson_lines(transformed)]
    via_file = [(line, raw) for _, line, raw in iter_ndjson_lines(part)]

    assert via_dir == via_file == [(1, b'{"a": 1}'), (2, b'{"a": 2}')]


def test_a_json_file_is_still_scanned_by_the_file_branch(tmp_path: Path) -> None:
    """The fix must not shut the legitimate single-file path — the upload hook
    relies on it."""
    part = tmp_path / "part-0.json"
    part.write_bytes(b'{"a": 1}\n')

    assert list(iter_ndjson_lines(part)) == [(str(part), 1, b'{"a": 1}')]


def test_the_suffix_check_is_case_insensitive(tmp_path: Path) -> None:
    part = tmp_path / "PART-0.JSON"
    part.write_bytes(b'{"a": 1}\n')

    assert list(iter_ndjson_lines(part)) == [(str(part), 1, b'{"a": 1}')]


def test_the_directory_branch_is_case_insensitive_too(tmp_path: Path) -> None:
    """The case fold has to live on both sides. A per-suffix glob is
    case-sensitive on POSIX, so an uppercase part would be read when the caller
    named the file and skipped when it named the parent directory — the same
    branch asymmetry this change exists to remove."""
    part = tmp_path / "PART-0.JSON"
    part.write_bytes(b'{"a": 1}\n')

    assert list(iter_ndjson_lines(tmp_path)) == [(str(part), 1, b'{"a": 1}')]


@pytest.mark.parametrize("suffix", [".json", ".jsonl", ".ndjson"])
def test_every_declared_suffix_is_read_by_the_file_branch(
    tmp_path: Path, suffix: str
) -> None:
    """`.jsonl` and `.ndjson` are the conventional names for the same
    one-record-per-line format, so an app following either convention must be
    validated rather than silently skipped."""
    part = tmp_path / f"part-0{suffix}"
    part.write_bytes(b'{"a": 1}\n')

    assert list(iter_ndjson_lines(part)) == [(str(part), 1, b'{"a": 1}')]


@pytest.mark.parametrize("suffix", [".json", ".jsonl", ".ndjson"])
def test_every_declared_suffix_is_read_by_the_directory_branch(
    tmp_path: Path, suffix: str
) -> None:
    """The whole point of the shared tuple: the two branches cannot disagree
    about which files count."""
    (tmp_path / f"part-0{suffix}").write_bytes(b'{"a": 1}\n')

    assert [raw for _, _, raw in iter_ndjson_lines(tmp_path)] == [b'{"a": 1}']


def test_mixed_suffixes_in_one_directory_are_all_read_and_globally_sorted(
    tmp_path: Path,
) -> None:
    """Ordering must be stable across the whole set, not grouped by extension —
    otherwise adding a second extension silently reorders every report."""
    (tmp_path / "a.json").write_bytes(b'{"n": 1}\n')
    (tmp_path / "b.jsonl").write_bytes(b'{"n": 2}\n')
    (tmp_path / "c.ndjson").write_bytes(b'{"n": 3}\n')
    (tmp_path / "d.json").write_bytes(b'{"n": 4}\n')

    assert [raw for _, _, raw in iter_ndjson_lines(tmp_path)] == [
        b'{"n": 1}',
        b'{"n": 2}',
        b'{"n": 3}',
        b'{"n": 4}',
    ]


def test_a_file_is_not_yielded_twice_when_suffixes_overlap(tmp_path: Path) -> None:
    """One walk filtered by a membership test, so a file matching the rule is
    visited exactly once however many suffixes are declared — a record can never
    be double-counted."""
    (tmp_path / "part-0.json").write_bytes(b'{"a": 1}\n')

    assert len(list(iter_ndjson_lines(tmp_path))) == 1


def test_the_suffix_tuple_is_the_single_source_for_both_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the drift this whole change exists to prevent. Asserting the
    tuple's literals would pass even if both branches hard-coded the same
    literals and ignored it, so swap in a suffix no branch could have
    hard-coded and assert both walks follow it — in *and* out."""
    from application_sdk.validation import ndjson as ndjson_module

    sentinel = tmp_path / "part-0.sentinelnd"
    sentinel.write_bytes(b'{"a": 1}\n')
    declared = tmp_path / "part-0.json"
    declared.write_bytes(b'{"a": 2}\n')

    monkeypatch.setattr(ndjson_module, "NDJSON_SUFFIXES", (".sentinelnd",))

    assert list(iter_ndjson_lines(sentinel)) == [(str(sentinel), 1, b'{"a": 1}')]
    assert list(iter_ndjson_lines(tmp_path)) == [(str(sentinel), 1, b'{"a": 1}')]
    assert list(iter_ndjson_lines(declared)) == []


def test_the_declared_suffixes_are_lower_cased_extensions() -> None:
    """The tuple is compared against ``Path.suffix.lower()``, so an entry that
    is not a lower-cased, dot-prefixed extension would silently never match."""
    from application_sdk.validation.ndjson import NDJSON_SUFFIXES

    assert NDJSON_SUFFIXES == (".json", ".jsonl", ".ndjson")
    assert all(s == s.lower() and s.startswith(".") for s in NDJSON_SUFFIXES)
