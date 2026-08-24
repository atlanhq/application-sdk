"""Format-agnostic artifact validation: the shared outcome surface (ADR-0020).

Data crosses app boundaries as files, and at every hand-off the producer's idea of
the artifact's shape and the consumer's idea of it are independent beliefs that
nothing checks. ``storage/integrity.py`` attests that the bytes read are the bytes
written and is explicit that this proves nothing about the artifact being
*semantically* complete; this is the missing third leg.

This module is the wrapper's **shared** half — the part that is identical no matter
what format the artifact is in or where its declaration came from:

* the logical-type vocabulary declarations are written against;
* the typed declaration a schema source resolves to;
* the outcome vocabulary;
* :class:`ArtifactValidationReport`, the one report shape;
* the bounded drill-down matrix and the outcome-event attribute map.

The two plug-in seams that vary — *where the declaration comes from* and *how a
format is checked* — are :mod:`application_sdk.validation.protocols`. Keeping them
separate is load-bearing: conflating them forces either a dataframe dependency or a
hand-authored field list for the 500-type asset case.

**Dependency floor.** Nothing in this module tree may import ``pyarrow``,
``pandas`` or ``pandera``. A JSON-only caller must never pay for a parquet reader,
and pandera stays test-only (ADR-0020, Option 2) — this is a standing constraint,
not a temporary state. ``tests/unit/validation/test_artifact_dependency_floor.py``
enforces it statically.

**Sync by design.** Nothing here is a coroutine. Validators are plain synchronous
scans so the one caller that needs to stay off the event loop — the activity
interceptor — owns the offload decision (``run_in_thread`` / an isolated child
process) rather than every validator re-deciding it.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Final, Literal, Union

import orjson

from application_sdk.constants import ARTIFACT_VALIDATION_MAX_ITEMS_PER_AXIS
from application_sdk.observability.events import ARTIFACT_VALIDATION_EVENT
from application_sdk.observability.logger_adaptor import (
    ARTIFACT_BOUNDARY_KEY,
    ARTIFACT_FAILED_KEY,
    ARTIFACT_FIELD_KEY,
    ARTIFACT_FIELDS_DECLARED_KEY,
    ARTIFACT_FORMAT_KEY,
    ARTIFACT_PASSED_KEY,
    ARTIFACT_SCHEMA_SOURCE_KEY,
    ARTIFACT_TOTAL_KEY,
    ARTIFACT_UNDECODABLE_KEY,
    ARTIFACT_UNIT_KEY,
    ARTIFACT_VALIDATION_MATRIX_KEY,
)

__all__ = [
    "ARTIFACT_FIELD_TYPES",
    "ARTIFACT_FIELD_TYPES_EXTENDED",
    "ARTIFACT_VALIDATION_EVENT",
    "ARTIFACT_VALIDATION_OUTCOMES",
    "OUTCOME_ABSENT",
    "OUTCOME_CLEAN",
    "OUTCOME_FLAGGED",
    "OUTCOME_NOT_DECLARED",
    "OUTCOME_UNSUPPORTED",
    "UNIT_COLUMN",
    "UNIT_RECORD",
    "ArtifactDeclaration",
    "ArtifactFailureKind",
    "ArtifactFieldType",
    "ArtifactFieldTypeExtended",
    "ArtifactValidationFailure",
    "ArtifactValidationOutcome",
    "ArtifactValidationReport",
    "DeclaredField",
    "FieldMapDeclaration",
    "ModelDeclaration",
    "artifact_validation_event_fields",
    "artifact_validation_matrix_json",
]


# ---------------------------------------------------------------------------
# The logical-type vocabulary
# ---------------------------------------------------------------------------
#
# The SDK owns the vocabulary; every field that uses it is the app's. Two tiers,
# the second layering additively on the first, mirroring the ``ArtifactFieldType``
# / ``ArtifactFieldTypeExtended`` typealiases the contract toolkit emits: the base
# is a floor every validator must map for every format, while an extension member
# may resolve to "unsupported for this format" without widening that floor.
#
# Each member earns its place against a specific observed failure rather than
# mirroring arrow wholesale. The load-bearing pair is ``string`` vs ``timestamp``:
# a production RCA traced a 73-day frozen lineage marker to one column that had
# become a string where the consumer expected a timestamp, with every workflow in
# the chain reporting success throughout. That single distinction is why this
# capability exists.

ArtifactFieldType = Literal[
    "string",
    "int",
    "float",
    "bool",
    "timestamp",
    "date",
    "json",
    "any",
]
"""The stable floor: types every validator must map, for every format."""

ArtifactFieldTypeExtended = Union[
    ArtifactFieldType,
    Literal["decimal", "binary", "time", "array", "struct", "map"],
]
"""Additive extension. Declarations use this, so a member can be declared before
every validator supports it — resolving to ``unsupported`` for a format rather
than widening the floor above."""

ARTIFACT_FIELD_TYPES: Final[frozenset[str]] = frozenset(
    {"string", "int", "float", "bool", "timestamp", "date", "json", "any"}
)
"""Runtime membership test for :data:`ArtifactFieldType`."""

ARTIFACT_FIELD_TYPES_EXTENDED: Final[frozenset[str]] = ARTIFACT_FIELD_TYPES | frozenset(
    {"decimal", "binary", "time", "array", "struct", "map"}
)
"""Runtime membership test for :data:`ArtifactFieldTypeExtended`."""


# ---------------------------------------------------------------------------
# What a schema source resolves to
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredField:
    """One field an app declares it requires of an artifact.

    Nested payloads are addressed by **dotted path plus a container type**, not by
    a recursive type grammar: ``payload.rows`` typed ``array`` rather than a nested
    schema literal. The deeply-nested case never needs the grammar because it
    delegates to an executable model (see :class:`ModelDeclaration`).
    """

    path: str
    """Field name, or dotted path for a nested field (e.g. ``payload.rows``)."""
    type: ArtifactFieldTypeExtended = "any"
    """Declared logical type. ``any`` means "must be present, type not asserted" —
    so a thin declaration can assert presence without anyone inventing a wrong type
    to satisfy the vocabulary."""
    required: bool = True
    """When False the field is type-checked only if present."""


@dataclass(frozen=True)
class FieldMapDeclaration:
    """A declaration resolved to an explicit field map — the ``ContractSource`` shape.

    Checked by diffing: declared names and logical types against what the artifact
    actually carries.
    """

    fields: tuple[DeclaredField, ...] = ()

    @property
    def field_count(self) -> int:
        """How many fields the declaration names."""
        return len(self.fields)


@dataclass(frozen=True)
class ModelDeclaration:
    """A declaration resolved to an executable typed model — the ``ModelSource`` shape.

    Nothing is authored: the model *is* the declaration, which is what makes the
    500-type / 4000-property asset case tractable. Checked by delegation to the
    model's own validation, never by diffing — a model carries no column mapping,
    which is why the parquet x model cell is genuinely ``unsupported`` and says so
    rather than guessing.
    """

    model: type

    @property
    def field_count(self) -> int:
        """Always 0: a model declares no enumerable field list at this layer."""
        return 0


ArtifactDeclaration = Union[FieldMapDeclaration, ModelDeclaration]
"""Tagged union of what a :class:`~application_sdk.validation.protocols.SchemaSource`
resolves to. A union rather than one struct with optional halves, so a validator
cannot silently treat "declared no fields" and "delegate to a model" as the same
thing."""


# ---------------------------------------------------------------------------
# The outcome vocabulary
# ---------------------------------------------------------------------------

OUTCOME_CLEAN: Final = "clean"
"""Checked against a declaration; nothing failed."""

OUTCOME_FLAGGED: Final = "flagged"
"""Checked against a declaration; at least one unit failed."""

OUTCOME_NOT_DECLARED: Final = "not_declared"
"""No declaration exists for this artifact. Carries ``boundary``: a finding on an
entrypoint's public interface, informational on an internal ``@task``."""

OUTCOME_UNSUPPORTED: Final = "unsupported"
"""A declaration exists but this (format x source) cell cannot check it — e.g.
parquet x model. Never silence."""

OUTCOME_ABSENT: Final = "absent"
"""The artifact itself was not readable: missing, empty, or a malformed declaration
artifact that degraded to a warning rather than an exception into the caller."""

ArtifactValidationOutcome = Literal[
    "clean", "flagged", "not_declared", "unsupported", "absent"
]
"""Every artifact hand-off emits exactly one of these — the negatives included.
A check that reports nothing is indistinguishable from a check that passed, and
that ambiguity is itself a defect: the earlier upload-time hook returned early and
emitted nothing when its path gate did not match, so an app could look adopted
while validating zero records."""

ARTIFACT_VALIDATION_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        OUTCOME_CLEAN,
        OUTCOME_FLAGGED,
        OUTCOME_NOT_DECLARED,
        OUTCOME_UNSUPPORTED,
        OUTCOME_ABSENT,
    }
)
"""Runtime membership test for :data:`ArtifactValidationOutcome`."""


ArtifactFailureKind = Literal["missing", "type_mismatch", "undecodable", "invalid"]
"""Why one unit failed. ``missing``/``type_mismatch`` come from a field-map diff,
``undecodable`` from a unit that could not be parsed at all, ``invalid`` from a
model delegating its own validation messages back."""

UNIT_RECORD: Final = "record"
"""Unit for the streaming per-record formats (NDJSON)."""

UNIT_COLUMN: Final = "column"
"""Unit for the metadata-only formats, where the footer schema is diffed and no row
is ever read (parquet)."""


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactValidationFailure:
    """One failing unit — a record that broke, or a column that disagreed."""

    kind: ArtifactFailureKind
    """Why it failed."""
    field: str = ""
    """Declared field path involved, or "" when the whole unit failed."""
    expected: str = ""
    """Declared logical type ("" when not a type question)."""
    actual: str = ""
    """Observed type ("" when not a type question, or unknown)."""
    file: str = ""
    """Artifact file the unit came from ("" when validated in-memory)."""
    line: int = 0
    """1-based record line within ``file``; 0 for column units and in-memory checks."""
    # ``dataclasses.field`` qualified: this class has a member named ``field``,
    # which shadows a bare ``field`` import inside the class body.
    errors: list[str] = dataclasses.field(default_factory=list)
    """Human-readable messages. A list, mirroring ``AssetValidationFailure.errors``,
    because a delegating model returns several at once."""


@dataclass
class ArtifactValidationReport:
    """Aggregate outcome of validating one artifact against one declaration.

    Mirrors :class:`~application_sdk.validation.assets.AssetValidationReport`:
    scalar counts, a bounded failure list, ``ok``/``failed``, and a
    ``format_report(*, max_items=...)`` renderer that shares its cap with the
    telemetry matrix.

    ``total``/``passed`` count **units**, named by :attr:`unit` — records for a
    streaming scan, columns for a footer diff — so the same four scalars describe
    both without a consumer inferring the unit from the format.

    The three non-scan outcomes have no counts to report and are built through
    :meth:`not_declared`, :meth:`unsupported` and :meth:`absent`, which pin
    :attr:`verdict`. Everything else derives its outcome from the failure list, so
    "reported clean" and "found nothing wrong" cannot come apart.
    """

    artifact_format: str = ""
    """``ndjson`` / ``parquet`` — "" when nothing was read."""
    schema_source: str = ""
    """``contract`` / ``model`` — "" when no declaration was resolved."""
    unit: str = ""
    """What ``total``/``passed`` count: :data:`UNIT_RECORD` or :data:`UNIT_COLUMN`."""
    fields_declared: int = 0
    """How many fields the declaration named (0 for a model declaration)."""
    total: int = 0
    """Units examined. Always the whole artifact — the scan is never sampled."""
    passed: int = 0
    """Units with no failure."""
    failures: list[ArtifactValidationFailure] = dataclasses.field(default_factory=list)
    """Every failing unit. Unbounded: only the *output* surfaces are capped."""
    boundary: bool = False
    """Whether this hand-off sits on an entrypoint's public interface."""
    reason: str = ""
    """Short explanation, chiefly for the non-scan outcomes."""
    verdict: ArtifactValidationOutcome | None = None
    """Pinned non-scan outcome. ``None`` means "a scan ran; derive it"."""

    # -- derived ---------------------------------------------------------

    @property
    def outcome(self) -> ArtifactValidationOutcome:
        """The single value the outcome event reports."""
        if self.verdict is not None:
            return self.verdict
        return OUTCOME_CLEAN if self.ok else OUTCOME_FLAGGED

    @property
    def ok(self) -> bool:
        """True when nothing failed.

        The non-scan outcomes are ``ok`` too: they are honest reports that no check
        ran, not verdicts against the artifact.
        """
        return not self.failures

    @property
    def failed(self) -> int:
        """Units that failed — ``total - passed``, never a row count.

        A record can break on several fields at once, so ``len(failures)`` is a
        problem-row count and can exceed this.
        """
        return self.total - self.passed

    @property
    def undecodable(self) -> int:
        """Units that could not be parsed at all.

        Derived from the failure list rather than tracked alongside it, so the two
        cannot disagree.
        """
        return sum(1 for f in self.failures if f.kind == "undecodable")

    # -- non-scan constructors -------------------------------------------

    @classmethod
    def not_declared(
        cls, *, boundary: bool, reason: str = "no artifact schema declared"
    ) -> "ArtifactValidationReport":
        """No declaration exists. A finding on the boundary, informational inside."""
        return cls(verdict=OUTCOME_NOT_DECLARED, boundary=boundary, reason=reason)

    @classmethod
    def unsupported(
        cls,
        *,
        artifact_format: str,
        schema_source: str,
        reason: str,
        boundary: bool = False,
    ) -> "ArtifactValidationReport":
        """This (format x source) cell cannot check the declaration it was given."""
        return cls(
            verdict=OUTCOME_UNSUPPORTED,
            artifact_format=artifact_format,
            schema_source=schema_source,
            reason=reason,
            boundary=boundary,
        )

    @classmethod
    def absent(
        cls,
        *,
        reason: str,
        artifact_format: str = "",
        schema_source: str = "",
        boundary: bool = False,
    ) -> "ArtifactValidationReport":
        """The artifact or its declaration could not be read at all."""
        return cls(
            verdict=OUTCOME_ABSENT,
            artifact_format=artifact_format,
            schema_source=schema_source,
            reason=reason,
            boundary=boundary,
        )

    # -- rendering -------------------------------------------------------

    def format_report(
        self, *, max_items: int = ARTIFACT_VALIDATION_MAX_ITEMS_PER_AXIS
    ) -> str:
        """Render a human-readable summary.

        ``max_items`` caps how many failures are *listed*, never how many are
        examined — the headline counts always reflect the whole artifact. Defaults
        to :data:`~application_sdk.constants.ARTIFACT_VALIDATION_MAX_ITEMS_PER_AXIS`,
        the same cap :func:`artifact_validation_matrix_json` applies to the
        structured telemetry, so the two surfaces stay in lockstep.
        """
        if self.verdict is not None:
            head = f"Artifact validation: {self.outcome}"
            if self.verdict == OUTCOME_NOT_DECLARED:
                head += " (boundary)" if self.boundary else " (internal)"
            return f"{head} — {self.reason}" if self.reason else head

        cell = f"{self.artifact_format or '?'} x {self.schema_source or '?'}"
        unit = self.unit or "unit"
        lines = [
            f"Artifact validation [{cell}]: {self.outcome} — "
            f"{self.passed}/{self.total} {unit}s passed, {self.failed} failed, "
            f"{self.undecodable} undecodable, {self.fields_declared} field(s) declared"
        ]
        lines.extend(f"  {_failure_line(f)}" for f in self.failures[:max_items])
        # Split the overflow by kind so a tail of undecodable records is never
        # miscounted as type mismatches — matching the disjoint headline.
        remaining = self.failures[max_items:]
        for kind in sorted({f.kind for f in remaining}):
            count = sum(1 for f in remaining if f.kind == kind)
            lines.append(f"  ... and {count} more {kind}")
        return "\n".join(lines)


def _failure_line(failure: ArtifactValidationFailure) -> str:
    """Render one failure row for :meth:`ArtifactValidationReport.format_report`."""
    label = failure.field or "<record>"
    loc = _location(failure.file, failure.line)
    detail = "; ".join(failure.errors)
    if failure.expected or failure.actual:
        types = f" (declared {failure.expected or '?'}, found {failure.actual or '?'})"
    else:
        types = ""
    suffix = f": {detail}" if detail else ""
    return f"{failure.kind.upper()} {label}{types}{loc}{suffix}"


def _location(file: str, line: int) -> str:
    if file and line:
        return f" ({file}:{line})"
    if file:
        return f" ({file})"
    return ""


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

_MATRIX_ERROR_MAXLEN: Final = 300
"""Per-row message cap (chars). A delegating model's messages can be long, so
truncate: a single row must not be able to bloat the ClickHouse attribute."""


def artifact_validation_matrix_json(
    report: ArtifactValidationReport,
    *,
    max_items: int = ARTIFACT_VALIDATION_MAX_ITEMS_PER_AXIS,
) -> str:
    """Compact per-failure drill-down for the outcome event, as one JSON string.

    Lands as a single ``LogAttributes`` value in ClickHouse so a consumer can
    ``JSONExtract`` the per-failure detail against workflow outcomes with no schema
    change (mirrors the preflight gate's ``check_matrix`` and the asset validator's
    ``asset_validation_matrix``). Small fixed fields only, bounded to ``max_items``
    rows by the same constant :meth:`ArtifactValidationReport.format_report` uses,
    so the human report and the telemetry can never drift.

    **Always returns a value — ``"[]"`` when there is nothing to show** — so
    consumers parse it unconditionally instead of branching on presence. A branch
    mishandled in the dropping direction is how a hand-off that never reached a
    verdict vanishes from the numerator it belongs in.
    """
    rows = [
        {
            "kind": f.kind,
            "field": f.field,
            "expected": f.expected,
            "actual": f.actual,
            "error": ("; ".join(f.errors))[:_MATRIX_ERROR_MAXLEN],
            "file": f.file,
            "line": f.line,
        }
        for f in report.failures[:max_items]
    ]
    return orjson.dumps(rows).decode()


def artifact_validation_event_fields(
    report: ArtifactValidationReport,
    *,
    artifact_field: str = "",
    max_items: int = ARTIFACT_VALIDATION_MAX_ITEMS_PER_AXIS,
) -> dict[str, str | int | bool]:
    """Build the outcome event's attribute map from a report.

    The single mapping site from report to telemetry, so the emitter cannot drift
    from the allowlist. Every key returned here is in
    ``logger_adaptor._KNOWN_EXTRA_KEYS``; anything absent from that allowlist is
    dropped by ``_build_extra_dict`` and silently never reaches OTLP, which is why
    ``tests/unit/validation/test_artifact_event_fields.py`` asserts this map against
    the allowlist rather than against an emit call.

    Every field is present on **every** outcome, negatives included — the matrix as
    ``"[]"``. Emit with::

        logger.info(
            ARTIFACT_VALIDATION_EVENT,
            app_name=...,
            entrypoint=...,
            **artifact_validation_event_fields(report, artifact_field=...),
        )

    Args:
        report: The report to project.
        artifact_field: Output-contract field name the artifact came from.
            Declarations are keyed by ``(entrypoint, field)``, so this plus the
            already-allowlisted ``entrypoint`` identifies the declaration exactly.
            Nothing is inferred from path shape — path-shape inference is precisely
            what made the earlier upload-time hook silently validate nothing.
        max_items: Row cap for the matrix; shared with ``format_report``.

    Returns:
        Keyword arguments for the ``ARTIFACT_VALIDATION_EVENT`` log call.
    """
    return {
        "outcome": report.outcome,
        "reason": report.reason,
        ARTIFACT_FORMAT_KEY: report.artifact_format,
        ARTIFACT_SCHEMA_SOURCE_KEY: report.schema_source,
        ARTIFACT_FIELD_KEY: artifact_field,
        ARTIFACT_UNIT_KEY: report.unit,
        ARTIFACT_TOTAL_KEY: report.total,
        ARTIFACT_PASSED_KEY: report.passed,
        ARTIFACT_FAILED_KEY: report.failed,
        ARTIFACT_UNDECODABLE_KEY: report.undecodable,
        ARTIFACT_FIELDS_DECLARED_KEY: report.fields_declared,
        ARTIFACT_BOUNDARY_KEY: report.boundary,
        ARTIFACT_VALIDATION_MATRIX_KEY: artifact_validation_matrix_json(
            report, max_items=max_items
        ),
    }
