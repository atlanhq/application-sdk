"""The public artifact-validation entry point (ADR-0020).

:func:`validate_artifact` is the thin, format-agnostic wrapper the whole capability
is named after. It does four things and nothing else:

1. asks a :class:`~application_sdk.validation.protocols.SchemaSource` for the
   declaration;
2. dispatches on the format the declaration carries;
3. hands the artifact to that format's
   :class:`~application_sdk.validation.protocols.FormatValidator`;
4. stamps the shared outcome fields onto whatever report comes back.

Everything expensive lives behind step 3, which is why a JSON-only caller never
loads a parquet reader.

**Every hand-off resolves to exactly one outcome, negatives included.** There is no
path through this function that returns nothing or raises: a missing declaration is
``not_declared``, a cell that cannot check the declaration it was handed is
``unsupported``, and an unreadable declaration or a validator that blew up is
``absent`` plus a warning. The earlier upload-time hook returned early and emitted
*nothing* when its path gate did not match, so an app could look adopted while
validating zero records — a check that reports nothing is indistinguishable from a
check that passed, and that ambiguity is itself the defect.

**It never raises into the caller.** The validation scaffold is defense in depth; a
check that breaks the hand-off it was added to protect is worse than no check at
all. Both plug-in seams are therefore wrapped: a source that raises, a validator
that raises, and a mis-shaped plug-in all degrade to ``absent`` with the reason
recorded, and the exception is logged at WARNING with a traceback rather than
propagated.

**Synchronous by design.** Validators are plain scans, so the one caller that must
stay off the event loop — the activity interceptor — owns the offload decision
(``run_in_thread``, or an isolated child process for the model path) rather than
every validator re-deciding it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.validation.artifacts import (
    ArtifactDeclaration,
    ArtifactValidationReport,
)
from application_sdk.validation.protocols import FormatValidator, SchemaSource
from application_sdk.validation.sources import ArtifactDeclarationError

logger = get_logger(__name__)

__all__ = ["builtin_format_validators", "validate_artifact"]


def builtin_format_validators() -> tuple[FormatValidator, ...]:
    """The format validators that ship with the SDK.

    A function rather than a module-level tuple so the imports stay **inside** it:
    the parquet validator's dependency floor is ``pyarrow``, an extra, and a
    module-level import here would drag it onto the import path of every JSON-only
    caller — exactly the cost coupling the two-seam design exists to prevent
    (``tests/unit/validation/test_artifact_dependency_floor.py`` enforces it).

    Empty until the per-format validators land (FND-688 NDJSON, FND-689 parquet).
    Until then every declared artifact resolves to ``unsupported`` naming the format
    that had no validator — which is the honest report, and is visible in the
    outcome events rather than looking like a pass.
    """
    return ()


def validate_artifact(
    path: Path | str,
    source: SchemaSource,
    *,
    validators: Sequence[FormatValidator] | None = None,
    boundary: bool = False,
) -> ArtifactValidationReport:
    """Validate one artifact against the declaration its source resolves to.

    Example::

        from application_sdk.validation import ContractSource, validate_artifact

        report = validate_artifact(
            local_path,
            ContractSource(field="raw_queries", entrypoint="extract"),
            boundary=True,
        )
        logger.info(
            ARTIFACT_VALIDATION_EVENT,
            **artifact_validation_event_fields(report, artifact_field="raw_queries"),
        )

    Args:
        path: The artifact on local disk — a file, or a directory for the formats
            that hand off a directory of parts.
        source: Where the declaration comes from. A
            :class:`~application_sdk.validation.sources.ContractSource` for a
            generated contract declaration, a
            :class:`~application_sdk.validation.sources.ModelSource` to delegate to
            a typed model. There is no inline source.
        validators: Format validators to dispatch across, first match on
            ``artifact_format`` winning. Defaults to
            :func:`builtin_format_validators`; pass an explicit sequence to add an
            app's own format or to pin the set under test.
        boundary: Whether this hand-off sits on an entrypoint's public interface.
            Carried onto the report and the outcome event, where it is what makes a
            missing declaration a finding rather than an informational note.

    Returns:
        The one shared report shape. Never ``None``, and this call never raises —
        see the module docstring.
    """
    source_kind = _safe_kind(source)

    if not isinstance(source, SchemaSource):
        return _plugin_broken(
            f"schema source {type(source).__name__} does not implement SchemaSource",
            schema_source=source_kind,
            boundary=boundary,
        )

    try:
        declaration = source.resolve()
    except ArtifactDeclarationError as exc:
        # Deliberately not `not_declared`: the app declared something, the SDK could
        # not read it. Reporting that as "you forgot to declare" would blame the app
        # on its own public boundary for the SDK's read failure.
        logger.warning(
            "Artifact validation: declaration unreadable — %s", exc, exc_info=True
        )
        return ArtifactValidationReport.absent(
            reason=f"declaration unreadable: {exc}",
            schema_source=source_kind,
            boundary=boundary,
        )
    except Exception as exc:  # noqa: BLE001 - a plug-in seam; nothing may escape
        logger.warning(
            "Artifact validation: %s source raised while resolving a declaration: %s",
            source_kind or type(source).__name__,
            exc,
            exc_info=True,
        )
        return ArtifactValidationReport.absent(
            reason=f"schema source raised: {type(exc).__name__}",
            schema_source=source_kind,
            boundary=boundary,
        )

    if declaration is None:
        return ArtifactValidationReport.not_declared(boundary=boundary)

    artifact_format = declaration.artifact_format
    validator = _select_validator(
        artifact_format,
        builtin_format_validators() if validators is None else validators,
    )
    if validator is None:
        return ArtifactValidationReport.unsupported(
            artifact_format=artifact_format,
            schema_source=source_kind,
            reason=(
                f"no validator registered for format {artifact_format!r}"
                if artifact_format
                else "the declaration names no format"
            ),
            boundary=boundary,
        )

    try:
        supported = validator.supports(declaration)
    except Exception as exc:  # noqa: BLE001 - a plug-in seam; nothing may escape
        logger.warning(
            "Artifact validation: %s validator raised from supports(): %s",
            artifact_format,
            exc,
            exc_info=True,
        )
        return _plugin_broken(
            f"validator raised from supports(): {type(exc).__name__}",
            artifact_format=artifact_format,
            schema_source=source_kind,
            boundary=boundary,
        )

    if not supported:
        # The one cell that answers False today is parquet x model: a model carries
        # no column mapping, so a footer diff has nothing to diff against. It says
        # so out loud rather than guessing or going quiet.
        return ArtifactValidationReport.unsupported(
            artifact_format=artifact_format,
            schema_source=source_kind,
            reason=(
                f"the {artifact_format} validator cannot check a "
                f"{source_kind or 'schema'}-sourced declaration"
            ),
            boundary=boundary,
        )

    try:
        report = validator.validate(Path(path), declaration)
    except Exception as exc:  # noqa: BLE001 - a plug-in seam; nothing may escape
        # "Our validator broke" — a different axis from "the artifact is
        # unverifiable", and this one always fails open regardless of posture.
        logger.warning(
            "Artifact validation: %s validator raised on %s: %s",
            artifact_format,
            path,
            exc,
            exc_info=True,
        )
        return _plugin_broken(
            f"validator raised: {type(exc).__name__}",
            artifact_format=artifact_format,
            schema_source=source_kind,
            boundary=boundary,
        )

    return _stamp(
        report,
        validator=validator,
        declaration=declaration,
        schema_source=source_kind,
        boundary=boundary,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _safe_kind(source: object) -> str:
    """``source.kind``, or "" when asking for it fails.

    ``kind`` is a property on a plug-in the SDK did not write, so reading it is
    itself inside the seam. An empty string is the report's own "no declaration was
    resolved" value, so a source too broken to name itself degrades into the shape
    the report already has a meaning for.
    """
    try:
        kind = source.kind  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - a plug-in seam; nothing may escape
        logger.warning(
            "Artifact validation: schema source %s raised from .kind: %s",
            type(source).__name__,
            exc,
            exc_info=True,
        )
        return ""
    return kind if isinstance(kind, str) else ""


def _select_validator(
    artifact_format: str, validators: Sequence[FormatValidator]
) -> FormatValidator | None:
    """First validator claiming ``artifact_format``, or ``None``.

    Mis-shaped plug-ins are skipped rather than raising: ``isinstance`` against a
    ``runtime_checkable`` protocol checks member *presence*, so this is the
    guardrail that turns "an app passed us the wrong object" into a reported
    ``unsupported`` instead of an ``AttributeError`` mid-scan.
    """
    if not artifact_format:
        return None
    for validator in validators:
        if not isinstance(validator, FormatValidator):
            logger.warning(
                "Artifact validation: ignoring %s — it does not implement "
                "FormatValidator",
                type(validator).__name__,
            )
            continue
        if validator.artifact_format == artifact_format:
            return validator
    return None


def _plugin_broken(
    reason: str,
    *,
    artifact_format: str = "",
    schema_source: str = "",
    boundary: bool,
) -> ArtifactValidationReport:
    """An ``absent`` report for "our own plug-in broke", never for a bad artifact."""
    return ArtifactValidationReport.absent(
        reason=reason,
        artifact_format=artifact_format,
        schema_source=schema_source,
        boundary=boundary,
    )


def _stamp(
    report: ArtifactValidationReport,
    *,
    validator: FormatValidator,
    declaration: ArtifactDeclaration,
    schema_source: str,
    boundary: bool,
) -> ArtifactValidationReport:
    """Set the shared outcome fields the wrapper owns, not the validator.

    A validator decides what failed; it does not get to disagree with the wrapper
    about which cell it is, what it counts, how many fields were declared, or
    whether the hand-off is on a public boundary. Those four are facts the wrapper
    already holds, so it writes them — a validator that forgot to set them, or set
    them inconsistently, cannot make the telemetry lie.
    """
    report.artifact_format = validator.artifact_format
    report.schema_source = schema_source
    report.unit = validator.unit
    report.fields_declared = declaration.field_count
    report.boundary = boundary
    return report
