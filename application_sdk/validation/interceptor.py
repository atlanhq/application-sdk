"""Both artifact-validation enforcement points, wired to the one seam (ADR-0020).

``FileReference`` is the universal hand-off token and the activity interceptor is
the one place every ``@task`` in every app funnels through, so both enforcement
points come off one declaration at one site::

      materialize_file_refs(...)     # durable -> local
    + validate(ingest)               <- consumer side, re-validate on read
      result = await task_method(input_data)
    + validate(handoff)              <- producer side, BEFORE persist: the bytes
      persist_file_refs(...)            are still local, so blame lands on the
                                        producer rather than on whoever reads it
                                        three hops later

The declaration is read off the contract field the reference was reached through,
exactly as the ``Lazy()`` marker is read inside the persist/materialise walk —
:func:`~application_sdk.storage.file_ref_sync.iter_named_file_refs` is that same
walk, and ``_find_file_refs`` is written in terms of it, so nothing here can drift
into disagreeing with the interceptor about which references a tree holds.

**No silent no-op — this is the point.** The earlier upload-time hook's path gate
returned ``None`` and emitted *nothing* when it did not match, so an app could look
adopted while validating zero records (FND-401). Here **every** reference on both
sides emits exactly one outcome event, negatives included: ``not_declared`` (with
its ``boundary`` attribute), ``unsupported``, and ``absent``. A check that reports
nothing is indistinguishable from a check that passed, and that ambiguity is itself
the defect.

**Warn-only, and it never raises into the task.**
:func:`~application_sdk.validation.wrapper.validate_artifact` already refuses to
raise across either plug-in seam; this module adds the outer belt so that a defect
in the *wiring* — the walk, the offload, the emit — cannot break a hand-off either.
Nothing here fails an activity, and nothing here blocks a persist. Whether a
verdict may block is a separate axis (the app's artifact-validation posture,
FND-692) that does not exist yet.

**Off the event loop.** Validators are plain synchronous scans, so per ADR-0020 the
interceptor — not each validator — owns the offload decision. Everything reachable
from here is contract-sourced, because this module only ever builds a
:class:`~application_sdk.validation.sources.ContractSource`: an NDJSON stream over
``orjson``, or a parquet footer read. Both ride
:func:`~application_sdk._runtime.offload.run_in_thread`. The model-sourced cell is
the one that needs a child process rather than a thread — its decode enters
third-party C extensions, where a native fault kills the worker instead of raising —
and it owns that isolation itself, inside the asset cell (FND-690). Nothing here has
to know about it, which is the point of the two-seam split.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Iterable, Iterator, Mapping

from application_sdk.observability.events import ARTIFACT_VALIDATION_EVENT
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.validation.artifacts import (
    ArtifactValidationReport,
    artifact_validation_event_fields,
)
from application_sdk.validation.sources import ArtifactDeclarationError, ContractSource

if TYPE_CHECKING:
    from application_sdk.app.registry import AppMetadata
    from application_sdk.storage.file_ref_sync import NamedFileRef

logger = get_logger(__name__)

__all__ = [
    "ARTIFACT_SIDES",
    "ARTIFACT_SIDE_HANDOFF",
    "ARTIFACT_SIDE_INGEST",
    "boundary_contract_types",
    "entrypoint_index",
    "validate_artifacts",
]


ARTIFACT_SIDE_INGEST: Final = "ingest"
"""Consumer side: the artifact was just materialised and is about to be read.

Stays on permanently once graduated — it is the only cover for producers that are
not our code, and for artifacts that were already written."""

ARTIFACT_SIDE_HANDOFF: Final = "handoff"
"""Producer side: the task has returned and the bytes are still local.

Checked *before* persist on purpose. The producing activity is still on the stack,
so a flag blames the app that wrote the artifact instead of the app that reads it
several hops later."""

ARTIFACT_SIDES: Final[frozenset[str]] = frozenset(
    {ARTIFACT_SIDE_INGEST, ARTIFACT_SIDE_HANDOFF}
)
"""Runtime membership test for the two enforcement points."""


# ---------------------------------------------------------------------------
# Worker-build resolution
# ---------------------------------------------------------------------------


def _app_metadata(app_name: str) -> "AppMetadata | None":
    """The registered app's metadata, or ``None`` when it cannot be looked up.

    One guarded lookup for both build-time resolvers below, so they cannot come to
    disagree about what an unresolvable app means.

    ``None`` is not an error here. ``create_activity_from_task`` is called directly
    by plenty of tests with nothing registered, and in a real worker the registry is
    populated long before ``create_worker`` runs — so a miss is either a harmless
    test path or an already-fatal boot problem that a warning from an advisory
    validation hook would only add noise to. DEBUG, with the traceback, so a third
    case nobody predicted is still recoverable from a log.

    Args:
        app_name: The registered app name, from the task's metadata.

    Returns:
        The app's metadata, or ``None``.
    """
    try:
        from application_sdk.app.registry import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports validation
            AppRegistry,
        )

        return AppRegistry.get_instance().get(app_name)
    except Exception:  # noqa: BLE001 — advisory wiring must never break worker build
        logger.debug(
            "Artifact validation: no registered app '%s' at worker build; hand-offs "
            "will report boundary=False and read the flat declaration file",
            app_name,
            exc_info=True,
        )
        return None


def boundary_contract_types(app_name: str) -> frozenset[type]:
    """Every contract class that sits on one of ``app_name``'s public boundaries.

    An entry point's ``input_type`` and ``output_type`` and nothing else — the same
    line :mod:`application_sdk.app._artifact_schema_guard` draws at registration,
    and for the same reason: an entry point's contracts are read by another app or
    by the DAG, while an internal ``@task`` contract is the app's own business. The
    default ``run()`` is registered as an *implicit* entry point carrying the same
    metadata, so "every entry point" already means "every public boundary" with no
    special case to drift.

    Resolved **once at worker build** and baked into the activity's closure, the
    same shape the preflight gate's posture uses, so ``boundary`` is accurate
    without a registry lookup on every hand-off.

    Membership is by exact class identity rather than ``issubclass``: a subclass of
    a boundary contract is a *different* contract, whose own declaration is keyed
    by its own entry point, and treating it as public would report a finding
    against an app for a field nobody outside it can see.

    Never raises. An app that is not registered — the case every direct
    ``create_activity_from_task`` call in a test hits — resolves to the empty set,
    so every hand-off reports ``boundary=False``. That is the conservative
    direction: it under-reports findings rather than inventing them.

    Args:
        app_name: The registered app name, from the task's metadata.

    Returns:
        The boundary contract classes, or an empty set when they cannot be
        resolved.
    """
    metadata = _app_metadata(app_name)
    if metadata is None:
        return frozenset()
    types: set[type] = set()
    for entry_point in metadata.entry_points.values():
        types.add(entry_point.input_type)
        types.add(entry_point.output_type)
    return frozenset(types)


def entrypoint_index(app_name: str) -> Mapping[str, str]:
    """Map every Temporal workflow type ``app_name`` registers to its entry point.

    Declarations are keyed by ``(entrypoint, contract field name)``, and the
    entry-point half decides *which file* is read: a bundle's declarations live at
    ``app/generated/<entry-point>/artifact_schemas.json``, a single-entry-point
    app's at the flat ``app/generated/artifact_schemas.json``. Without this,
    every bundle would miss its own declarations and report ``not_declared``
    fleet-wide — a silent no-op wearing an outcome event's clothes.

    Built from ``AppMetadata.workflow_types``, which is the same index the worker
    registers workflow classes from, so a run's ``activity.info().workflow_type``
    is always a key here. Legacy inbound aliases resolve to the same entry point as
    the canonical type.

    Resolved once at worker build for the same reason as
    :func:`boundary_contract_types`, and equally never raises: an unresolvable app
    yields an empty mapping, and every hand-off then reads the flat file, which is
    the correct answer for the single-entry-point majority.

    Args:
        app_name: The registered app name, from the task's metadata.

    Returns:
        ``{workflow type: entry-point name}``, possibly empty.
    """
    metadata = _app_metadata(app_name)
    if metadata is None:
        return {}
    return {
        workflow_type: entry_point.name
        for workflow_type, entry_point in metadata.workflow_types.items()
    }


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


async def validate_artifacts(
    data: Any,
    *,
    side: str,
    app_name: str = "",
    entrypoint: str = "",
    boundary_contracts: frozenset[type] = frozenset(),
) -> None:
    """Validate every ``FileReference`` in ``data`` and emit one outcome each.

    Called twice per task — once with the materialised input, once with the
    returned output before it is persisted. Warn-only and total: every reference
    reachable in the tree produces exactly one
    :data:`~application_sdk.observability.events.ARTIFACT_VALIDATION_EVENT`,
    including the references that turn out to have no declaration, no supported
    validator, or no readable artifact.

    References are de-duplicated on ``(owner, field, local_path)``. Two elements of
    one ``list[FileReference]`` pointing at the same file share one declaration and
    would emit byte-identical rows, so scanning the file twice would only inflate
    the denominator FND-694's graduation review reads.

    Never raises, never blocks, and never fails the activity — including when the
    walk, the offload or the emit is what broke. A check that breaks the hand-off it
    was added to protect is worse than no check at all.

    Args:
        data: The task's input (at ingest) or output (at hand-off).
        side: :data:`ARTIFACT_SIDE_INGEST` or :data:`ARTIFACT_SIDE_HANDOFF`.
        app_name: Registered app name, carried onto the event.
        entrypoint: Entry-point name for this run, from :func:`entrypoint_index`.
            "" reads the flat generated declaration file.
        boundary_contracts: From :func:`boundary_contract_types`, resolved at
            worker build.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — deferred so a deployment can flip the switch under test, mirroring VALIDATE_ASSETS_ON_UPLOAD
        VALIDATE_ARTIFACTS,
    )

    if not VALIDATE_ARTIFACTS:
        return

    try:
        named = list(_unique(_walk(data)))
    except Exception:  # noqa: BLE001 — the scaffold may not break the hand-off
        logger.warning(
            "Artifact validation: could not walk the %s payload; skipping it "
            "(the hand-off continues)",
            side,
            exc_info=True,
        )
        return

    for item in named:
        boundary = item.owner is not None and item.owner in boundary_contracts
        try:
            report = await _report_for(item, entrypoint=entrypoint, boundary=boundary)
        except Exception:  # noqa: BLE001 — the scaffold may not break the hand-off
            logger.warning(
                "Artifact validation: the %s check for field '%s' raised; "
                "reporting it as absent (the hand-off continues)",
                side,
                item.field,
                exc_info=True,
            )
            report = ArtifactValidationReport.absent(
                reason="the artifact-validation hook raised",
                boundary=boundary,
            )
        _emit(
            report,
            side=side,
            app_name=app_name,
            entrypoint=entrypoint,
            artifact_field=item.field,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _walk(data: Any) -> Iterator["NamedFileRef"]:
    """The persist/materialise walk, re-used rather than re-implemented."""
    from application_sdk.storage.file_ref_sync import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        iter_named_file_refs,
    )

    return iter_named_file_refs(data)


def _unique(named: Iterable["NamedFileRef"]) -> Iterator["NamedFileRef"]:
    """Drop references that would emit an identical row for an identical scan."""
    seen: set[tuple[type | None, str, str | None]] = set()
    for item in named:
        key = (item.owner, item.field, item.ref.local_path)
        if key in seen:
            continue
        seen.add(key)
        yield item


async def _report_for(
    item: "NamedFileRef", *, entrypoint: str, boundary: bool
) -> ArtifactValidationReport:
    """Resolve one reference to exactly one report.

    Nothing is inferred from the shape of the storage path — path-shape inference is
    precisely what made the earlier upload-time hook match nothing. The contract
    field the reference was reached through is what the declaration is keyed on, and
    the walk already knows it.
    """
    from application_sdk._runtime.offload import (  # noqa: PLC0415 — circular: _runtime loads observability which loads constants
        run_in_thread,
    )
    from application_sdk.validation.wrapper import (  # noqa: PLC0415 — keeps the parquet validator's pyarrow floor off a JSON-only caller's import path
        validate_artifact,
    )

    source = ContractSource(field=item.field, entrypoint=entrypoint)
    local_path = item.ref.local_path
    if not local_path:
        return _no_local_artifact(source, boundary=boundary)
    # Synchronous by design (ADR-0020): the scan is a plain streaming read, so the
    # interceptor owns the offload rather than every validator re-deciding it.
    return await run_in_thread(
        validate_artifact, Path(local_path), source, boundary=boundary
    )


def _no_local_artifact(
    source: ContractSource, *, boundary: bool
) -> ArtifactValidationReport:
    """The outcome for a reference carrying no local artifact to scan.

    A lazy field the interceptor deliberately did not download, a durable reference
    that was never materialised, an output field left unset. There is nothing to
    read, but silence is the one answer that is not allowed, so the declaration is
    resolved anyway to keep the two facts apart: "this artifact has no declaration"
    is ``not_declared`` and is a finding only on a boundary, while "it is declared
    and we could not read it" is ``absent``. Collapsing them would report an app
    that declared its artifact correctly as having declared nothing.
    """
    try:
        declaration = source.resolve()
    except ArtifactDeclarationError as exc:
        logger.warning(
            "Artifact validation: declaration unreadable — %s", exc, exc_info=True
        )
        return ArtifactValidationReport.absent(
            reason=f"declaration unreadable: {exc}",
            schema_source=source.kind,
            boundary=boundary,
        )
    if declaration is None:
        return ArtifactValidationReport.not_declared(boundary=boundary)
    return ArtifactValidationReport.absent(
        reason="the reference carries no local artifact to check",
        artifact_format=declaration.artifact_format,
        schema_source=source.kind,
        boundary=boundary,
    )


def _emit(
    report: ArtifactValidationReport,
    *,
    side: str,
    app_name: str,
    entrypoint: str,
    artifact_field: str,
) -> None:
    """Emit the one queryable row, plus a readable WARNING when it is flagged.

    Wrapped end to end: a defect in the emit path — a matrix that will not encode,
    an adapter that rejects a kwarg — must not be able to break the hand-off the
    row was describing.
    """
    try:
        # conformance: ignore[L018] app_name/entrypoint are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion, and a pinned outcome event exists precisely so its attributes are queryable columns
        logger.info(
            ARTIFACT_VALIDATION_EVENT,
            app_name=app_name,
            entrypoint=entrypoint,
            **artifact_validation_event_fields(
                report, artifact_field=artifact_field, side=side
            ),
        )
        if not report.ok:
            logger.warning(
                "Artifact validation flagged the %s hand-off of '%s' "
                "(the hand-off continues): %s",
                side,
                artifact_field or "<unnamed reference>",
                report.format_report(),
            )
    except Exception:  # noqa: BLE001 — the scaffold may not break the hand-off
        logger.warning(
            "Artifact validation: could not emit the %s outcome for field '%s' "
            "(the hand-off continues)",
            side,
            artifact_field,
            exc_info=True,
        )
