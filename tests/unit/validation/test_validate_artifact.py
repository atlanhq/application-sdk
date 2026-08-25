"""Tests for the public ``validate_artifact`` entry point (ADR-0020, FND-686).

Two invariants carry the whole capability, and both are the kind that fail
quietly:

* **Every hand-off emits exactly one outcome, negatives included.** A check that
  reports nothing is indistinguishable from a check that passed. The earlier
  upload-time hook returned early and emitted nothing when its path gate did not
  match, so an app could look adopted while validating zero records — every test
  here asserts an outcome, never an absence.
* **Nothing escapes into the caller.** The scaffold is defense in depth; a check
  that breaks the hand-off it was added to protect is worse than no check. Both
  plug-in seams are exercised with plug-ins that raise, and with plug-ins of the
  wrong shape entirely.

The last test walks the real loader over real contract-toolkit output, so the
committed fixture is exercised end to end rather than only at the source seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application_sdk.validation.artifacts import (
    ARTIFACT_FORMATS,
    ARTIFACT_VALIDATION_OUTCOMES,
    FORMAT_NDJSON,
    FORMAT_PARQUET,
    OUTCOME_ABSENT,
    OUTCOME_CLEAN,
    OUTCOME_FLAGGED,
    OUTCOME_NOT_DECLARED,
    OUTCOME_UNSUPPORTED,
    UNIT_RECORD,
    ArtifactDeclaration,
    ArtifactValidationFailure,
    ArtifactValidationReport,
    DeclaredField,
    FieldMapDeclaration,
    ModelDeclaration,
)
from application_sdk.validation.sources import (
    ARTIFACT_SCHEMAS_FILENAME,
    ArtifactDeclarationError,
    ContractSource,
    ModelSource,
    _load_schemas,
)
from application_sdk.validation.wrapper import (
    builtin_format_validators,
    validate_artifact,
)

FIXTURE = Path(__file__).parent / "resources" / ARTIFACT_SCHEMAS_FILENAME

DECLARATION = FieldMapDeclaration(
    fields=(
        DeclaredField(path="QUERY_ID", type="string"),
        DeclaredField(path="START_TIME", type="timestamp"),
    ),
    artifact_format=FORMAT_NDJSON,
)


# ---------------------------------------------------------------------------
# Test doubles for the two seams
# ---------------------------------------------------------------------------


class _StubSource:
    """A schema source whose answer the test dictates."""

    def __init__(
        self, answer: ArtifactDeclaration | None = None, raises: Exception | None = None
    ) -> None:
        self._answer = answer
        self._raises = raises

    @property
    def kind(self) -> str:
        return "contract"

    def resolve(self) -> ArtifactDeclaration | None:
        if self._raises is not None:
            raise self._raises
        return self._answer


class _StubValidator:
    """A format validator whose behaviour the test dictates."""

    def __init__(
        self,
        *,
        artifact_format: str = FORMAT_NDJSON,
        unit: str = UNIT_RECORD,
        supports: bool = True,
        report: ArtifactValidationReport | None = None,
        raises: Exception | None = None,
        raises_from_supports: Exception | None = None,
    ) -> None:
        self.artifact_format = artifact_format
        self.unit = unit
        self._supports = supports
        self._report = report
        self._raises = raises
        self._raises_from_supports = raises_from_supports
        self.seen: list[Path] = []

    def supports(self, declaration: ArtifactDeclaration) -> bool:
        if self._raises_from_supports is not None:
            raise self._raises_from_supports
        return self._supports

    def validate(
        self, path: Path, declaration: ArtifactDeclaration
    ) -> ArtifactValidationReport:
        self.seen.append(path)
        if self._raises is not None:
            raise self._raises
        return self._report or ArtifactValidationReport(total=3, passed=3)


def _assert_emits(report: ArtifactValidationReport, outcome: str) -> None:
    """Every path resolves to exactly one outcome from the fixed vocabulary."""
    assert report.outcome in ARTIFACT_VALIDATION_OUTCOMES
    assert report.outcome == outcome


# ---------------------------------------------------------------------------
# No declaration
# ---------------------------------------------------------------------------


def test_no_declaration_is_not_declared(tmp_path: Path) -> None:
    report = validate_artifact(tmp_path / "a.json", _StubSource(None))
    _assert_emits(report, OUTCOME_NOT_DECLARED)


def test_boundary_is_what_makes_a_missing_declaration_a_finding(tmp_path: Path) -> None:
    """Same fact, two readings — a finding on the public interface, a note inside."""
    on_boundary = validate_artifact(tmp_path, _StubSource(None), boundary=True)
    internal = validate_artifact(tmp_path, _StubSource(None), boundary=False)

    assert on_boundary.boundary is True
    assert internal.boundary is False
    assert "(boundary)" in on_boundary.format_report()
    assert "(internal)" in internal.format_report()


# ---------------------------------------------------------------------------
# The source seam degrades, never raises
# ---------------------------------------------------------------------------


def test_unreadable_declaration_is_absent_not_not_declared(tmp_path: Path) -> None:
    """A loader failure must never be reported as an app that forgot to declare.

    ``not_declared`` on a public boundary is a finding *against the app*. Reporting
    the SDK's own read failure that way would blame an app for a file it wrote
    correctly — which is why the two answers have separate channels.
    """
    report = validate_artifact(
        tmp_path,
        _StubSource(raises=ArtifactDeclarationError(message="envelope version 2")),
        boundary=True,
    )

    _assert_emits(report, OUTCOME_ABSENT)
    assert report.outcome != OUTCOME_NOT_DECLARED
    assert "envelope version 2" in report.reason
    assert report.schema_source == "contract"
    assert report.boundary is True


def test_a_source_that_blows_up_fails_open(tmp_path: Path) -> None:
    report = validate_artifact(tmp_path, _StubSource(raises=RuntimeError("boom")))

    _assert_emits(report, OUTCOME_ABSENT)
    assert "RuntimeError" in report.reason


def test_a_mis_shaped_source_is_reported_not_raised(tmp_path: Path) -> None:
    """``isinstance`` against a runtime protocol checks member presence — a guardrail.

    An app that passes the wrong object gets a named reason on the outcome event
    rather than an ``AttributeError`` surfacing from inside a scan.
    """
    # Deliberate: an object that is not a SchemaSource is exactly what this
    # guardrail exists for, so the checker's objection is the scenario.
    report = validate_artifact(tmp_path, object())  # type: ignore[arg-type]

    _assert_emits(report, OUTCOME_ABSENT)
    assert "does not implement SchemaSource" in report.reason


def test_a_source_whose_kind_raises_still_reports(tmp_path: Path) -> None:
    class _Hostile:
        @property
        def kind(self) -> str:
            raise RuntimeError("even the name is broken")

        def resolve(self) -> None:
            return None

    report = validate_artifact(tmp_path, _Hostile())
    assert report.outcome in ARTIFACT_VALIDATION_OUTCOMES
    assert report.schema_source == ""


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_no_validator_for_the_format_is_unsupported(tmp_path: Path) -> None:
    """Never silence: the gap is named, and it names the format that had none."""
    report = validate_artifact(
        tmp_path,
        _StubSource(DECLARATION),
        validators=[_StubValidator(artifact_format=FORMAT_PARQUET)],
    )

    _assert_emits(report, OUTCOME_UNSUPPORTED)
    assert FORMAT_NDJSON in report.reason
    assert report.artifact_format == FORMAT_NDJSON


def test_the_builtins_carry_every_format_the_adr_names() -> None:
    """NDJSON (FND-688) and parquet (FND-689) both ship. Neither is silently absent.

    Asserted as a set against the format vocabulary rather than as a hand-written
    list, so adding a format to :data:`ARTIFACT_FORMATS` without registering its
    validator fails here instead of resolving to ``unsupported`` in production.
    """
    assert {v.artifact_format for v in builtin_format_validators()} == ARTIFACT_FORMATS


def test_a_format_with_no_builtin_validator_says_so_out_loud(tmp_path: Path) -> None:
    """The invariant outlives the gap it was first written for.

    Both formats now ship, but the SDK deliberately does not police the format
    vocabulary at load time: a newer toolkit can declare a format this SDK has no
    validator for, and the honest answer is ``unsupported`` *naming the format* —
    never ``absent`` (which would claim the declaration was unreadable) and never
    silence (which would read as a pass).
    """
    from_a_newer_toolkit = FieldMapDeclaration(
        fields=(DeclaredField(path="QUERY_ID", type="string"),),
        artifact_format="avro",
    )
    report = validate_artifact(tmp_path, _StubSource(from_a_newer_toolkit))

    _assert_emits(report, OUTCOME_UNSUPPORTED)
    assert "no validator registered" in report.reason
    assert "avro" in report.reason


def test_the_builtin_parquet_validator_is_wired_end_to_end(tmp_path: Path) -> None:
    """The default validator set really dispatches, not just reports its members."""
    pytest.importorskip("pyarrow")

    report = validate_artifact(
        tmp_path,
        _StubSource(
            FieldMapDeclaration(
                fields=(DeclaredField(path="QUERY_ID", type="string"),),
                artifact_format=FORMAT_PARQUET,
            )
        ),
    )

    # Empty directory: no part to read, so the honest answer is `absent` — reached
    # through the builtin validator rather than through "no validator registered".
    _assert_emits(report, OUTCOME_ABSENT)
    assert report.artifact_format == FORMAT_PARQUET
    assert "no parquet file" in report.reason


def test_a_declaration_with_no_format_is_unsupported(tmp_path: Path) -> None:
    report = validate_artifact(
        tmp_path,
        _StubSource(FieldMapDeclaration(fields=(DeclaredField(path="a"),))),
        validators=[_StubValidator()],
    )

    _assert_emits(report, OUTCOME_UNSUPPORTED)
    assert "names no format" in report.reason


@pytest.mark.parametrize("artifact_format", [FORMAT_NDJSON, FORMAT_PARQUET])
def test_a_field_map_naming_zero_fields_is_unsupported(
    tmp_path: Path, artifact_format: str
) -> None:
    """A scan over zero fields finds nothing, so it would derive ``clean``.

    That is the "looks adopted, validates zero records" state this whole capability
    exists to remove, so it is rejected before dispatch rather than left for each
    format to remember. ``ContractSource`` cannot produce one — the loader refuses a
    zero-field schema and the toolkit refuses to generate one — but a custom
    ``SchemaSource`` is a supported plug-in, and this is the seam it comes in
    through. Asserted for both formats because the guard's whole point is that a
    format inherits it rather than re-implementing it.
    """
    validator = _StubValidator(artifact_format=artifact_format)

    report = validate_artifact(
        tmp_path,
        _StubSource(FieldMapDeclaration(fields=(), artifact_format=artifact_format)),
        validators=[validator],
    )

    _assert_emits(report, OUTCOME_UNSUPPORTED)
    assert "zero fields" in report.reason
    assert report.artifact_format == artifact_format
    # Rejected *before* dispatch: the validator was never handed the artifact, so no
    # format can turn an empty declaration into a pass on its own.
    assert validator.seen == []


def test_a_model_declaration_is_not_mistaken_for_an_empty_field_map(
    tmp_path: Path,
) -> None:
    """``ModelDeclaration.field_count`` is always 0 — that must stay dispatchable.

    A model enumerates no fields at this layer; delegating to it is exactly what it
    is for. Guarding the zero-field rejection on ``field_count`` instead of on the
    declaration type would silently take the whole NDJSON x model cell — the 500-type
    asset case — offline.
    """
    validator = _StubValidator()

    report = validate_artifact(
        tmp_path,
        _StubSource(ModelDeclaration(model=_StubValidator)),
        validators=[validator],
    )

    _assert_emits(report, OUTCOME_CLEAN)
    assert report.fields_declared == 0
    assert validator.seen == [tmp_path]


def test_parquet_times_model_is_unsupported(tmp_path: Path) -> None:
    """The one cell that genuinely cannot be checked says so rather than guessing.

    A model carries no column mapping, so a footer diff has nothing to diff
    against. The cell answering ``False`` is the design working, not a gap.
    """
    report = validate_artifact(
        tmp_path,
        ModelSource(model=_StubValidator, artifact_format=FORMAT_PARQUET),
        validators=[_StubValidator(artifact_format=FORMAT_PARQUET, supports=False)],
    )

    _assert_emits(report, OUTCOME_UNSUPPORTED)
    assert report.schema_source == "model"
    assert report.artifact_format == FORMAT_PARQUET


def test_a_mis_shaped_validator_is_skipped(tmp_path: Path) -> None:
    report = validate_artifact(
        tmp_path,
        _StubSource(DECLARATION),
        # Deliberate: a validator of the wrong shape is the scenario under test.
        validators=[object()],  # type: ignore[list-item]
    )

    _assert_emits(report, OUTCOME_UNSUPPORTED)


def test_a_source_resolving_to_a_non_declaration_is_reported(tmp_path: Path) -> None:
    """`isinstance` against a runtime protocol never checked `resolve()`'s return.

    A structurally-matching source can hand back anything at all, and the wrapper
    dereferences `.artifact_format` on whatever it gets. That read has to sit behind
    a check, or a plug-in the SDK did not write raises straight into the hand-off
    this function exists not to break.
    """
    report = validate_artifact(
        tmp_path,
        _StubSource("just a string"),  # type: ignore[arg-type]
        validators=[_StubValidator()],
    )

    _assert_emits(report, OUTCOME_ABSENT)
    assert "not an artifact declaration" in report.reason


def test_a_validator_whose_format_property_raises_is_skipped(tmp_path: Path) -> None:
    """The membership check itself runs plug-in code on 3.11 — see ``_implements``.

    `isinstance` against a runtime protocol reaches every member with `hasattr` on
    3.11, so a raising `@property` propagates out of the check that exists to stop
    exactly that. 3.12 reads the descriptor without invoking it, which is why this
    only ever went red on one leg of the matrix.
    """

    class _Hostile:
        @property
        def artifact_format(self) -> str:
            raise RuntimeError("nope")

        unit = UNIT_RECORD

        def supports(self, declaration: ArtifactDeclaration) -> bool:
            return True

        def validate(
            self, path: Path, declaration: ArtifactDeclaration
        ) -> ArtifactValidationReport:
            return ArtifactValidationReport()

    report = validate_artifact(
        tmp_path, _StubSource(DECLARATION), validators=[_Hostile()]
    )

    _assert_emits(report, OUTCOME_UNSUPPORTED)


def test_a_validator_returning_a_non_report_is_reported(tmp_path: Path) -> None:
    report = validate_artifact(
        tmp_path,
        _StubSource(DECLARATION),
        validators=[_StubValidator(report="clean")],  # type: ignore[arg-type]
    )

    _assert_emits(report, OUTCOME_ABSENT)
    assert "not a report" in report.reason


def test_a_validator_whose_unit_raises_on_the_way_out(tmp_path: Path) -> None:
    """The last two plug-in reads happen after the scan; they are in the seam too.

    A complete, correct report would still be thrown away by an ``AttributeError``
    raised while the wrapper asks the validator to name its own cell.
    """

    class _BrokenUnit:
        artifact_format = FORMAT_NDJSON

        @property
        def unit(self) -> str:
            raise RuntimeError("no unit")

        def supports(self, declaration: ArtifactDeclaration) -> bool:
            return True

        def validate(
            self, path: Path, declaration: ArtifactDeclaration
        ) -> ArtifactValidationReport:
            return ArtifactValidationReport(total=5, passed=5)

    report = validate_artifact(
        tmp_path, _StubSource(DECLARATION), validators=[_BrokenUnit()]
    )

    # Which negative outcome depends on the interpreter, and asserting one would
    # pin this to a version rather than to the behaviour. On 3.11 the protocol
    # membership check invokes `unit` and rejects the plug-in before it ever runs
    # (`unsupported`); on 3.12+ the check is inert, the scan completes, and the
    # raise lands in `_stamp` (`absent`). The invariant is the same on both, and it
    # is the one that matters: a broken plug-in never yields a pass, and never
    # raises into the caller.
    assert report.outcome in {OUTCOME_ABSENT, OUTCOME_UNSUPPORTED}
    assert report.outcome != OUTCOME_CLEAN


def test_first_matching_validator_wins(tmp_path: Path) -> None:
    first = _StubValidator()
    second = _StubValidator()

    validate_artifact(tmp_path, _StubSource(DECLARATION), validators=[first, second])

    assert first.seen and not second.seen


# ---------------------------------------------------------------------------
# The scan path
# ---------------------------------------------------------------------------


def test_a_clean_scan_reports_clean(tmp_path: Path) -> None:
    report = validate_artifact(
        tmp_path / "a.json", _StubSource(DECLARATION), validators=[_StubValidator()]
    )

    _assert_emits(report, OUTCOME_CLEAN)
    assert report.total == 3
    assert report.passed == 3


def test_a_failing_scan_reports_flagged(tmp_path: Path) -> None:
    scan = ArtifactValidationReport(
        total=10,
        passed=9,
        failures=[
            ArtifactValidationFailure(
                kind="type_mismatch",
                field="START_TIME",
                expected="timestamp",
                actual="string",
            )
        ],
    )
    report = validate_artifact(
        tmp_path,
        _StubSource(DECLARATION),
        validators=[_StubValidator(report=scan)],
    )

    _assert_emits(report, OUTCOME_FLAGGED)
    assert report.failed == 1


def test_the_wrapper_owns_the_shared_fields(tmp_path: Path) -> None:
    """A validator decides what failed; it does not get to contradict the wrapper.

    Cell, unit, declared-field count and boundary are facts the wrapper already
    holds, so it writes them — a validator that forgot them, or set them
    inconsistently, cannot make the telemetry lie.
    """
    scan = ArtifactValidationReport(
        artifact_format="wrong",
        schema_source="wrong",
        unit="wrong",
        fields_declared=99,
        total=1,
        passed=1,
    )
    report = validate_artifact(
        tmp_path,
        _StubSource(DECLARATION),
        validators=[_StubValidator(report=scan)],
        boundary=True,
    )

    assert report.artifact_format == FORMAT_NDJSON
    assert report.schema_source == "contract"
    assert report.unit == UNIT_RECORD
    assert report.fields_declared == DECLARATION.field_count == 2
    assert report.boundary is True


def test_a_model_declaration_declares_no_fields(tmp_path: Path) -> None:
    report = validate_artifact(
        tmp_path,
        _StubSource(ModelDeclaration(model=_StubValidator)),
        validators=[_StubValidator()],
    )

    assert report.schema_source == "contract"
    assert report.fields_declared == 0


def test_a_string_path_is_accepted(tmp_path: Path) -> None:
    validator = _StubValidator()
    validate_artifact(
        str(tmp_path / "a.json"), _StubSource(DECLARATION), validators=[validator]
    )

    assert validator.seen == [tmp_path / "a.json"]


# ---------------------------------------------------------------------------
# The validator seam degrades, never raises
# ---------------------------------------------------------------------------


def test_a_validator_that_blows_up_fails_open(tmp_path: Path) -> None:
    """ "Our validator broke" is a separate axis from "the artifact is unverifiable",
    and this one always fails open regardless of posture."""
    report = validate_artifact(
        tmp_path,
        _StubSource(DECLARATION),
        validators=[_StubValidator(raises=MemoryError("scan died"))],
    )

    _assert_emits(report, OUTCOME_ABSENT)
    assert "MemoryError" in report.reason
    assert report.artifact_format == FORMAT_NDJSON


def test_a_validator_that_blows_up_in_supports_fails_open(tmp_path: Path) -> None:
    report = validate_artifact(
        tmp_path,
        _StubSource(DECLARATION),
        validators=[_StubValidator(raises_from_supports=ValueError("nope"))],
    )

    _assert_emits(report, OUTCOME_ABSENT)
    assert "supports()" in report.reason


# ---------------------------------------------------------------------------
# End to end over real contract-toolkit output
# ---------------------------------------------------------------------------


def test_end_to_end_over_the_generated_fixture(tmp_path: Path) -> None:
    """Real generated declaration file -> real loader -> dispatch -> report.

    The declaration is never hand-built anywhere in this path, so the fixture
    exercises exactly what an adopted app ships.
    """
    _load_schemas.cache_clear()
    (tmp_path / ARTIFACT_SCHEMAS_FILENAME).write_bytes(FIXTURE.read_bytes())
    validator = _StubValidator(artifact_format=FORMAT_PARQUET, unit="column")

    report = validate_artifact(
        tmp_path / "raw_queries",
        ContractSource(field="raw_queries", generated_dir=tmp_path),
        validators=[validator],
        boundary=True,
    )

    _assert_emits(report, OUTCOME_CLEAN)
    assert report.artifact_format == FORMAT_PARQUET
    assert report.schema_source == "contract"
    assert report.unit == "column"
    assert report.fields_declared == 6
    assert report.boundary is True


def test_end_to_end_when_the_field_is_undeclared(tmp_path: Path) -> None:
    _load_schemas.cache_clear()
    (tmp_path / ARTIFACT_SCHEMAS_FILENAME).write_bytes(FIXTURE.read_bytes())

    report = validate_artifact(
        tmp_path / "something_else",
        ContractSource(field="something_else", generated_dir=tmp_path),
        validators=[_StubValidator()],
        boundary=True,
    )

    _assert_emits(report, OUTCOME_NOT_DECLARED)
    assert report.boundary is True


def test_end_to_end_over_a_malformed_file(tmp_path: Path) -> None:
    _load_schemas.cache_clear()
    (tmp_path / ARTIFACT_SCHEMAS_FILENAME).write_text("{not json")

    report = validate_artifact(
        tmp_path / "raw_queries",
        ContractSource(field="raw_queries", generated_dir=tmp_path),
        validators=[_StubValidator()],
    )

    _assert_emits(report, OUTCOME_ABSENT)
    assert "not valid JSON" in report.reason


@pytest.mark.parametrize(
    "source",
    [
        _StubSource(None),
        _StubSource(DECLARATION),
        _StubSource(raises=ArtifactDeclarationError(message="bad")),
        _StubSource(raises=RuntimeError("worse")),
        ModelSource(model=_StubValidator, artifact_format=FORMAT_PARQUET),
        ModelSource(model=int),
    ],
    ids=[
        "none",
        "declared",
        "unreadable",
        "exploding",
        "parquet-x-model",
        "no-validate",
    ],
)
def test_every_path_emits_exactly_one_outcome(tmp_path: Path, source: object) -> None:
    """The no-silent-no-op invariant, swept across every branch of the wrapper."""
    report = validate_artifact(
        tmp_path,
        # Deliberate: the sweep includes deliberately broken sources, so the
        # parameter is typed `object` and widened here at the one call site.
        source,  # type: ignore[arg-type]
        validators=[_StubValidator(artifact_format=FORMAT_PARQUET, supports=False)],
    )

    assert report.outcome in ARTIFACT_VALIDATION_OUTCOMES
