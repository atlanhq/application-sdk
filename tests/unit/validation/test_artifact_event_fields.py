"""The artifact-validation outcome event must actually reach OTLP.

Emitting a queryable event is a three-part contract — a pinned name constant, an
attribute-key constant, and entries in ``_KNOWN_EXTRA_KEYS`` — and getting two of
the three right fails *silently*: loguru accepts the kwargs, the log line looks
correct locally, and ``_build_extra_dict`` drops the unlisted keys on the floor
before the exporter ever sees them.

So these tests assert against the allowlist and against ``_build_extra_dict``
itself, not against a mocked emit call. A mock records whatever it is handed; only
the filter can tell you the field survives.
"""

from __future__ import annotations

import orjson

from application_sdk.observability.events import (
    ARTIFACT_VALIDATION_EVENT,
    OUTCOME_EVENT_NAMES,
)
from application_sdk.observability.logger_adaptor import (
    _KNOWN_EXTRA_KEYS,
    _build_extra_dict,
)
from application_sdk.validation.artifacts import (
    ArtifactValidationFailure,
    ArtifactValidationReport,
    artifact_validation_event_fields,
)


def _flagged() -> ArtifactValidationReport:
    return ArtifactValidationReport(
        artifact_format="ndjson",
        schema_source="contract",
        unit="record",
        fields_declared=4,
        total=1000,
        passed=999,
        boundary=True,
        failures=[
            ArtifactValidationFailure(
                kind="type_mismatch",
                field="START_TIME",
                expected="timestamp",
                actual="string",
                file="query_history.ndjson",
                line=41,
                errors=["declared timestamp, found string"],
            )
        ],
    )


_REPORTS: dict[str, ArtifactValidationReport] = {
    "flagged": _flagged(),
    "clean": ArtifactValidationReport(
        artifact_format="ndjson",
        schema_source="contract",
        unit="record",
        fields_declared=4,
        total=1000,
        passed=1000,
    ),
    "not_declared": ArtifactValidationReport.not_declared(boundary=True),
    "unsupported": ArtifactValidationReport.unsupported(
        artifact_format="parquet",
        schema_source="model",
        reason="a model carries no column mapping",
    ),
    "absent": ArtifactValidationReport.absent(reason="artifact not found"),
    # Shares its outcome with the row above and must not share its posture answer
    # — the whole reason the classification axis exists (FND-692).
    "validator_broken": ArtifactValidationReport.absent(
        reason="validator raised: RuntimeError", validator_broken=True
    ),
}


def test_every_event_key_is_on_the_allowlist() -> None:
    for outcome, report in _REPORTS.items():
        fields = artifact_validation_event_fields(
            report, artifact_field="query_history"
        )
        unlisted = set(fields) - _KNOWN_EXTRA_KEYS
        assert not unlisted, f"{outcome}: keys dropped before OTLP: {sorted(unlisted)}"


def test_every_event_field_survives_build_extra_dict() -> None:
    """The real filter, not a mock: this is what runs between loguru and the exporter."""
    for outcome, report in _REPORTS.items():
        fields = artifact_validation_event_fields(
            report, artifact_field="query_history"
        )
        survived = _build_extra_dict(dict(fields))
        assert survived == fields, f"{outcome}: {set(fields) - set(survived)} dropped"


def test_the_attribute_set_is_identical_across_outcomes() -> None:
    """Including the negatives — a consumer never branches on which keys exist."""
    shapes = {
        outcome: sorted(artifact_validation_event_fields(report))
        for outcome, report in _REPORTS.items()
    }
    assert len(set(map(tuple, shapes.values()))) == 1, shapes


def test_matrix_is_present_on_every_outcome() -> None:
    for outcome, report in _REPORTS.items():
        fields = artifact_validation_event_fields(report)
        matrix = fields["artifact_validation_matrix"]
        assert isinstance(matrix, str)
        assert isinstance(orjson.loads(matrix), list), outcome
    assert (
        artifact_validation_event_fields(_REPORTS["clean"])[
            "artifact_validation_matrix"
        ]
        == "[]"
    )


def test_fields_project_the_report_faithfully() -> None:
    fields = artifact_validation_event_fields(
        _flagged(), artifact_field="query_history"
    )
    assert fields["outcome"] == "flagged"
    assert fields["artifact_format"] == "ndjson"
    assert fields["artifact_schema_source"] == "contract"
    assert fields["artifact_field"] == "query_history"
    assert fields["artifact_unit"] == "record"
    assert fields["artifact_total"] == 1000
    assert fields["artifact_passed"] == 999
    assert fields["artifact_failed"] == 1
    assert fields["artifact_undecodable"] == 0
    assert fields["artifact_fields_declared"] == 4
    assert fields["boundary"] is True


def test_the_posture_axes_are_projected() -> None:
    """The three FND-692 keys ride the same single mapping site as the rest, so a
    row is self-describing without a join back to the boot-time posture event."""
    fields = artifact_validation_event_fields(
        _flagged(), artifact_field="query_history", mode="hard", enforcement="blocked"
    )
    assert fields["artifact_classification"] == "verdict"
    assert fields["artifact_validation_mode"] == "hard"
    assert fields["artifact_enforcement"] == "blocked"


def test_a_broken_validator_is_distinguishable_from_a_missing_artifact() -> None:
    """Both are ``absent``; only the classification separates them, and only one
    of them is allowed to block."""
    broken = artifact_validation_event_fields(_REPORTS["validator_broken"])
    missing = artifact_validation_event_fields(_REPORTS["absent"])
    assert broken["outcome"] == missing["outcome"] == "absent"
    assert broken["artifact_classification"] == "validator_broken"
    assert missing["artifact_classification"] == "artifact_unverifiable"


def test_the_posture_axes_default_to_empty_for_a_caller_with_no_posture() -> None:
    """ "" rather than "soft": a caller outside the interceptor has no posture, and
    reporting one nobody declared would inflate the soft denominator."""
    fields = artifact_validation_event_fields(_REPORTS["clean"])
    assert fields["artifact_validation_mode"] == ""
    assert fields["artifact_enforcement"] == ""


def test_boundary_is_reported_on_every_outcome_not_just_not_declared() -> None:
    internal = artifact_validation_event_fields(
        ArtifactValidationReport.not_declared(boundary=False)
    )
    assert internal["boundary"] is False
    assert "boundary" in artifact_validation_event_fields(_REPORTS["clean"])


def test_event_name_is_pinned_in_the_registry() -> None:
    assert ARTIFACT_VALIDATION_EVENT == "Artifact validation outcome"
    assert ARTIFACT_VALIDATION_EVENT in OUTCOME_EVENT_NAMES
