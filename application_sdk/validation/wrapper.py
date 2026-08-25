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
``not_declared``, a cell that cannot check the declaration it was handed —
or a field map that names no fields, which would report as declared while asserting
nothing — is ``unsupported``, and an unreadable declaration or a validator that blew
up is ``absent`` plus a warning. The earlier upload-time hook returned early and emitted
*nothing* when its path gate did not match, so an app could look adopted while
validating zero records — a check that reports nothing is indistinguishable from a
check that passed, and that ambiguity is itself the defect.

**Two axes, not one.** Every plumbing failure in this module degrades to
``absent``, which it shares with the honest "the artifact was not there" — so the
outcome alone cannot tell them apart, and an app that opted into blocking would be
failed by a defect in the SDK's own check. ``ArtifactValidationReport``'s second
axis carries the difference: everything built through ``_plugin_broken`` is
classified ``validator_broken`` and always fails open, whatever the app's posture
(ADR-0020, FND-692). Nothing outside this module's guard rails ever sets it — a
validator reporting on whether *it* broke is the one report that has to come from
outside it.

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
    FieldMapDeclaration,
    ModelDeclaration,
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
    Importing the parquet *module* is cheap: it defers ``pyarrow`` to the moment a
    parquet artifact is actually validated, and degrades to skip-with-warning when
    the extra is not installed.

    Both formats ADR-0020 names now ship: NDJSON (FND-688) streams line by line,
    parquet (FND-689) diffs a footer. A format neither claims still resolves to
    ``unsupported`` naming it — the honest report, visible in the outcome events
    rather than looking like a pass.

    Two validators, three cells. NDJSON claims **both** of its — the field-map diff
    and the model delegation folded in from the asset validator (FND-690) — because
    dispatch is by format, so one validator claims ``ndjson`` and then decides per
    declaration kind. Parquet x model stays genuinely ``unsupported``: a model
    carries no column mapping, so a footer diff has nothing to diff against.
    """
    from application_sdk.validation.ndjson import (  # noqa: PLC0415 — deferred on purpose: the parquet import below must stay inside this function, and both are resolved together
        NdjsonValidator,
    )
    from application_sdk.validation.parquet import (  # noqa: PLC0415 — deferred: keeps the parquet reader off a JSON-only caller's import path
        ParquetFooterValidator,
    )

    return (NdjsonValidator(), ParquetFooterValidator())


def validate_artifact(
    path: Path | str,
    source: SchemaSource,
    *,
    validators: Sequence[FormatValidator] | None = None,
    boundary: bool = False,
) -> ArtifactValidationReport:
    """Validate one artifact against the declaration its source resolves to.

    Example::

        from application_sdk.observability.events import ARTIFACT_VALIDATION_EVENT
        from application_sdk.observability.logger_adaptor import get_logger
        from application_sdk.validation import (
            ContractSource,
            artifact_validation_event_fields,
            validate_artifact,
        )

        logger = get_logger(__name__)

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

    if not _implements(source, SchemaSource):
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
            validator_broken=True,
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
            validator_broken=True,
        )

    if declaration is None:
        return ArtifactValidationReport.not_declared(boundary=boundary)

    if not isinstance(declaration, (FieldMapDeclaration, ModelDeclaration)):
        # `isinstance` against a runtime protocol never checked what `resolve()`
        # *returns*, only that the method exists. A structurally-matching source can
        # hand back anything, and reading `.artifact_format` off it would raise
        # straight into the hand-off this function exists not to break.
        logger.warning(
            "Artifact validation: %s source resolved to %s, not an artifact "
            "declaration",
            source_kind or type(source).__name__,
            type(declaration).__name__,
        )
        return _plugin_broken(
            f"schema source resolved to {type(declaration).__name__}, not an "
            f"artifact declaration",
            schema_source=source_kind,
            boundary=boundary,
        )

    # Safe from here: a dataclass attribute on a type this module owns.
    artifact_format = declaration.artifact_format

    if isinstance(declaration, FieldMapDeclaration) and not declaration.fields:
        # A field map naming nothing reports as *declared* while asserting nothing —
        # the exact "looks adopted, validates zero records" state this capability
        # exists to remove. `ContractSource` cannot produce one (`_parse_schemas`
        # refuses to load a zero-field schema, and the toolkit refuses to generate
        # one), but a custom `SchemaSource` is a supported plug-in, and every format
        # would otherwise have to remember to check this for itself: a scan over
        # zero fields finds nothing, so it derives `clean`.
        #
        # `unsupported` rather than `not_declared`: the app *did* declare something.
        # Blaming it for a missing declaration would be a different, wrong report.
        # Guarded on `FieldMapDeclaration` specifically, never on `field_count`,
        # because `ModelDeclaration.field_count` is always 0 — a model enumerates no
        # fields at this layer and delegating to it is exactly what it is for.
        return ArtifactValidationReport.unsupported(
            artifact_format=artifact_format,
            schema_source=source_kind,
            reason=(
                "the declaration names zero fields, so it would report as declared "
                "while checking nothing"
            ),
            boundary=boundary,
        )

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

    if not isinstance(report, ArtifactValidationReport):
        logger.warning(
            "Artifact validation: %s validator returned %s, not a report",
            artifact_format,
            type(report).__name__,
        )
        return _plugin_broken(
            f"validator returned {type(report).__name__}, not a report",
            artifact_format=artifact_format,
            schema_source=source_kind,
            boundary=boundary,
        )

    try:
        return _stamp(
            report,
            validator=validator,
            declaration=declaration,
            schema_source=source_kind,
            boundary=boundary,
        )
    except Exception as exc:  # noqa: BLE001 - a plug-in seam; nothing may escape
        # `_stamp` reads `artifact_format` and `unit` off the validator. They are
        # properties on a plug-in the SDK did not write, so the last two reads in
        # this function are inside the seam too — the report is complete and would
        # still be thrown away by an AttributeError raised on the way out.
        logger.warning(
            "Artifact validation: %s validator raised while naming its cell: %s",
            artifact_format,
            exc,
            exc_info=True,
        )
        return _plugin_broken(
            f"validator raised while naming its cell: {type(exc).__name__}",
            artifact_format=artifact_format,
            schema_source=source_kind,
            boundary=boundary,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _implements(candidate: object, protocol: type) -> bool:
    """``isinstance`` against a runtime protocol, with the check itself guarded.

    **The membership test runs the plug-in's own code on Python 3.11.** There,
    ``_ProtocolMeta.__instancecheck__`` reaches every protocol member with
    ``hasattr``, so a member declared as a ``@property`` is *invoked*, and anything
    it raises other than ``AttributeError`` propagates straight out of ``isinstance``.
    Python 3.12 switched to ``inspect.getattr_static``, which reads the descriptor
    without calling it, so the identical call is inert there — which is exactly how
    this stayed invisible until the 3.11 leg of the matrix went red.

    The guardrail that exists to stop a broken plug-in from reaching the hand-off
    cannot itself be the thing that raises into it, so it is wrapped on every
    version rather than on the one that currently needs it.

    A raise is treated as "not a usable plug-in" — the same answer a missing member
    gets, because the consequence is the same: the SDK cannot rely on it.

    One consequence worth knowing when reading the tests: on 3.11 a plug-in with a
    raising property is rejected *here*, while on 3.12+ it survives this check and
    is caught at the later read. The outcome differs (``unsupported`` vs ``absent``)
    but the invariant does not — a broken plug-in never yields a pass, and never
    raises into the caller.
    """
    try:
        return isinstance(candidate, protocol)
    except Exception as exc:  # noqa: BLE001 - a plug-in seam; nothing may escape
        logger.warning(
            "Artifact validation: %s raised while being checked against %s: %s",
            type(candidate).__name__,
            protocol.__name__,
            exc,
            exc_info=True,
        )
        return False


def _safe_kind(source: object) -> str:
    """``source.kind``, or "" when asking for it fails.

    ``kind`` is a property on a plug-in the SDK did not write, so reading it is
    itself inside the seam. An empty string is the report's own "no declaration was
    resolved" value, so a source too broken to name itself degrades into the shape
    the report already has a meaning for.
    """
    try:
        # Deliberate: this probes a plug-in that may not have `kind` at all — that
        # is the case being handled, not a type the checker should have to accept.
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

    Mis-shaped plug-ins are skipped rather than raising: a ``runtime_checkable``
    protocol checks member *presence*, so this is the guardrail that turns "an app
    passed us the wrong object" into a reported ``unsupported`` instead of an
    ``AttributeError`` mid-scan. The check goes through :func:`_implements` because
    on 3.11 it invokes the plug-in's properties and can raise on its own.
    """
    if not artifact_format:
        return None
    for validator in validators:
        if not _implements(validator, FormatValidator):
            logger.warning(
                "Artifact validation: ignoring %s — it does not implement "
                "FormatValidator",
                type(validator).__name__,
            )
            continue
        try:
            claimed = validator.artifact_format
        except Exception as exc:  # noqa: BLE001 - a plug-in seam; nothing may escape
            logger.warning(
                "Artifact validation: ignoring %s — its artifact_format raised: %s",
                type(validator).__name__,
                exc,
                exc_info=True,
            )
            continue
        if claimed == artifact_format:
            return validator
    return None


def _plugin_broken(
    reason: str,
    *,
    artifact_format: str = "",
    schema_source: str = "",
    boundary: bool,
) -> ArtifactValidationReport:
    """An ``absent`` report for "our own plug-in broke", never for a bad artifact.

    ``validator_broken=True`` is what carries that distinction past this function:
    every plumbing failure degrades to ``absent``, so the outcome alone cannot
    tell "we could not read the artifact" from "we fell over trying". The second
    axis can, and it is what keeps a hard-mode app from being failed by a defect
    in the SDK's own check (FND-692) — this classification always fails open.
    """
    return ArtifactValidationReport.absent(
        reason=reason,
        artifact_format=artifact_format,
        schema_source=schema_source,
        boundary=boundary,
        validator_broken=True,
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

    Two of the four are read off the validator, so **this can raise** if a plug-in's
    ``artifact_format`` or ``unit`` property misbehaves. The caller catches it: these
    are the last two reads inside the plug-in seam, and a complete report would
    otherwise be thrown away on the way out.
    """
    report.artifact_format = validator.artifact_format
    report.schema_source = schema_source
    report.unit = validator.unit
    report.fields_declared = declaration.field_count
    report.boundary = boundary
    return report
