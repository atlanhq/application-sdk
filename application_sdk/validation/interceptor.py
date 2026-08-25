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

**Only a deliberate posture can fail a hand-off.**
:func:`~application_sdk.validation.wrapper.validate_artifact` already refuses to
raise across either plug-in seam; this module adds the outer belt so that a defect
in the *wiring* — the walk, the offload, the emit — cannot break a hand-off either.
The single exception is the app's own opt-in: with
:attr:`~application_sdk.app.base.App.artifact_validation_mode` resolved to
``"hard"`` at worker build, a blockable outcome raises
:class:`ArtifactValidationBlockedError` and fails the activity. Soft is the
default and the only posture an app gets without asking, and it emits
``artifact_enforcement="would_block"`` instead — the loud, queryable forecast of
what hard mode would have done, which is what makes graduating a measured
decision rather than a leap (FND-694).

Two things stay outside that posture entirely. **A defect in this check always
fails open**, in either mode: everything the wrapper's guard rails classify
``validator_broken`` proceeds, because a check that breaks the hand-off it was
added to protect is worse than no check at all. And an *undeclared* artifact off a
public boundary never blocks, because ADR-0020 makes declaration optional on
app-internal ``@task`` contracts on purpose.

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

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Iterable, Iterator, Mapping

from application_sdk.errors.leaves import DataIntegrityError
from application_sdk.observability.events import (
    ARTIFACT_VALIDATION_EVENT,
    ARTIFACT_VALIDATION_POSTURE_EVENT,
)
from application_sdk.observability.logger_adaptor import ARTIFACT_MODE_KEY, get_logger
from application_sdk.validation.artifacts import (
    ENFORCEMENT_BLOCKED,
    ArtifactValidationReport,
    artifact_enforcement,
    artifact_validation_event_fields,
    artifact_validation_mode,
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
    "ArtifactValidationBlockedError",
    "artifact_validation_enforced",
    "boundary_contract_types",
    "entrypoint_index",
    "log_artifact_validation_posture",
    "resolve_artifact_enforcement",
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


def resolve_artifact_enforcement(app_cls: type | None) -> bool:
    """Resolve one app's artifact-validation posture. ``True`` = hard.

    Precedence, mirroring :func:`~application_sdk.execution._temporal.worker._resolve_gate_enforcement`
    exactly because an operator should not have to learn two rules:

    1. ``ATLAN_ARTIFACT_VALIDATION_MODE`` — the deploy-time ops lever, so a fleet
       that starts flagging can be stood down without an app release;
    2. the app's declared :attr:`~application_sdk.app.base.App.artifact_validation_mode`
       — the git-blamed opt-in;
    3. soft.

    **Only the literal** ``"hard"`` **enforces.** Every other set value — a typo, a
    ``"true"``, a ``"HARD "`` with a stray space (stripped and lowered, so that one
    does enforce) — falls back to soft. Blocking is always something someone chose,
    and the failure direction of a mistake is "reported but not blocked".

    An empty or unset env value is *not* an override: it falls through to the
    declared attribute. That is what lets a deployment leave the variable
    unset rather than having to spell the app's own default back at it.

    Never raises. An unresolvable app (``None``) resolves to soft, which is the
    conservative direction — the same one :func:`boundary_contract_types` takes.

    Args:
        app_cls: The registered app class, or ``None`` when it cannot be resolved.

    Returns:
        ``True`` for hard mode, ``False`` for soft.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — deferred so a deployment can flip the lever under test, mirroring VALIDATE_ARTIFACTS
        ARTIFACT_VALIDATION_MODE_ENV,
    )

    val = os.environ.get(ARTIFACT_VALIDATION_MODE_ENV)
    if val:
        return val.strip().lower() == "hard"
    declared = getattr(app_cls, "artifact_validation_mode", "soft")
    return str(declared).strip().lower() == "hard"


def artifact_validation_enforced(app_name: str) -> bool:
    """:func:`resolve_artifact_enforcement` for a registered app name.

    The form the activity seam needs: ``create_activity_from_task`` holds a task's
    ``app_name``, not its class. Resolved **once at worker build** and baked into
    the activity closure alongside :func:`boundary_contract_types` and
    :func:`entrypoint_index`, so a posture cannot change under a running worker
    and no hand-off pays a registry lookup for it.

    Routed through the same one rule as the worker's own posture emit, so the row
    an app's boot event reports is by construction the posture its activities run.

    Args:
        app_name: The registered app name, from the task's metadata.

    Returns:
        ``True`` for hard mode, ``False`` for soft (including an unregistered app).
    """
    metadata = _app_metadata(app_name)
    return resolve_artifact_enforcement(None if metadata is None else metadata.app_cls)


def log_artifact_validation_posture(
    app_name: str, *, enforce: bool, enabled: bool
) -> None:
    """Emit the boot-time posture row for one app — **every** app, soft included.

    The point is a complete denominator. An app whose tasks hand off no artifacts,
    or whose worker never runs one, emits no outcome row at all, so from outcomes
    alone a hard-mode app that has never blocked anything is indistinguishable from
    one that is not registered. This row is what makes adoption and posture drift
    measurable rather than a code-search artifact, and it is why the soft rows —
    the overwhelming majority — are the ones that matter most here.

    ``enabled=False`` reports :data:`~application_sdk.validation.artifacts.MODE_OFF`
    rather than the declared posture: a hard-mode app on a deployment with
    ``ATLAN_VALIDATE_ARTIFACTS`` down blocks nothing, and a row promising
    enforcement that is not happening is worse than no row.

    Mirrors ``preflight_gate.log_gate_posture`` down to the split from the
    human-facing hard-mode boot warning: this body is a pinned contract string that
    must never be reworded, that one is prose an operator reads.

    Args:
        app_name: The registered app name.
        enforce: Whether the app resolved to hard mode.
        enabled: Whether artifact validation runs at all on this deployment.
    """
    # conformance: ignore[L018] app_name is in _KNOWN_EXTRA_KEYS and the mode key is a pinned attribute; %-style would lose the OTLP promotion this event exists for
    logger.info(
        ARTIFACT_VALIDATION_POSTURE_EVENT,
        app_name=app_name,
        **{
            ARTIFACT_MODE_KEY: artifact_validation_mode(
                enforce=enforce, enabled=enabled
            )
        },
    )


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class ArtifactValidationBlockedError(DataIntegrityError):
    """A hard-mode app's artifact validation failed a hand-off.

    Raised **only** when the app resolved to
    :attr:`~application_sdk.app.base.App.artifact_validation_mode` ``"hard"`` and
    the outcome was one a posture is allowed to block on
    (:attr:`~application_sdk.validation.artifacts.ArtifactValidationReport.enforceable`).
    In soft mode the identical outcome emits ``artifact_enforcement="would_block"``
    and the hand-off proceeds.

    ``DATA_INTEGRITY`` is the category and ``APP_OWNER`` the audience for the same
    reason ``ArtifactDeclarationError`` uses them: the artifact and the declaration
    it disagreed with both belong to the app. Not retryable — an artifact that does
    not match its declaration does not start matching it on a second attempt, and
    retrying would only multiply the blast radius the producer-side check exists to
    keep at one workflow.

    Raised from inside the activity, so the SDK's standard translation stamps it
    onto ``ApplicationError.details[0]`` as ``FailureDetails``: the red activity
    pane, Temporal history and the Automation Engine all get the field, the side
    and the declared-vs-found detail without anyone parsing a message string.
    """

    code: ClassVar[str] = "DATA_INTEGRITY_ARTIFACT_VALIDATION"
    suggested_action: str | None = (
        "Fix the artifact so it matches the schema the app declared for that "
        "contract field, or correct the declaration in contract/app.pkl and "
        "regenerate. To stop blocking while the disagreement is investigated, set "
        "ATLAN_ARTIFACT_VALIDATION_MODE=soft on the deployment — the check keeps "
        "reporting and the run proceeds."
    )


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
    enforce: bool = False,
) -> None:
    """Validate every ``FileReference`` in ``data`` and emit one outcome each.

    Called twice per task — once with the materialised input, once with the
    returned output before it is persisted. Total: every reference reachable in the
    tree produces exactly one
    :data:`~application_sdk.observability.events.ARTIFACT_VALIDATION_EVENT`,
    including the references that turn out to have no declaration, no supported
    validator, or no readable artifact.

    References are de-duplicated on ``(owner, field, local_path)``. Two elements of
    one ``list[FileReference]`` pointing at the same file share one declaration and
    would emit byte-identical rows, so scanning the file twice would only inflate
    the denominator FND-694's graduation review reads.

    **Raises only under a deliberate posture.** With ``enforce=False`` — the default
    and every app's starting point — this never raises, never blocks and never
    fails the activity, including when the walk, the offload or the emit is what
    broke. With ``enforce=True`` a blockable outcome raises
    :class:`ArtifactValidationBlockedError`, and *only* a blockable one: everything
    the wrapper classified ``validator_broken`` still proceeds, because a check that
    breaks the hand-off it was added to protect is worse than no check at all.

    **Every reference emits before any of them blocks.** The loop runs to the end
    and the raise happens after it, so a flagged first artifact cannot silence the
    references behind it — the same no-silent-no-op rule that makes the negative
    outcomes emit at all. Blocking early would make an app's row count depend on
    which artifact failed, and that count is the denominator FND-694 reads.

    Args:
        data: The task's input (at ingest) or output (at hand-off).
        side: :data:`ARTIFACT_SIDE_INGEST` or :data:`ARTIFACT_SIDE_HANDOFF`.
        app_name: Registered app name, carried onto the event.
        entrypoint: Entry-point name for this run, from :func:`entrypoint_index`.
            "" reads the flat generated declaration file.
        boundary_contracts: From :func:`boundary_contract_types`, resolved at
            worker build.
        enforce: The app's posture, from :func:`artifact_validation_enforced` and
            resolved once at worker build. ``True`` blocks; ``False`` emits
            ``would_block`` and proceeds.

    Raises:
        ArtifactValidationBlockedError: ``enforce=True`` and at least one reference
            reached a blockable outcome.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — deferred so a deployment can flip the switch under test, mirroring VALIDATE_ASSETS_ON_UPLOAD
        VALIDATE_ARTIFACTS,
    )

    if not VALIDATE_ARTIFACTS:
        return

    mode = artifact_validation_mode(enforce=enforce)

    try:
        named = list(_unique(_walk(data)))
    except Exception:  # noqa: BLE001 — the walk is our own plumbing, so it fails open in either posture
        logger.warning(
            "Artifact validation: could not walk the %s payload; skipping it "
            "(the hand-off continues)",
            side,
            exc_info=True,
        )
        return

    blocked: list[tuple[str, ArtifactValidationReport]] = []
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
            # `validator_broken`: this is the hook itself failing, not the
            # artifact. Classified so even a hard-mode app proceeds — a defect
            # here may not fail a healthy run.
            report = ArtifactValidationReport.absent(
                reason="the artifact-validation hook raised",
                boundary=boundary,
                validator_broken=True,
            )
        # The one site that decides, so the value emitted and the value acted on
        # are the same value: `blocked` and `would_block` cannot come to disagree
        # about which outcomes are blockable, which is what makes the soft rows a
        # forecast of hard mode rather than a guess at it.
        enforcement = artifact_enforcement(report, enforce=enforce)
        _emit(
            report,
            side=side,
            app_name=app_name,
            entrypoint=entrypoint,
            artifact_field=item.field,
            mode=mode,
            enforcement=enforcement,
        )
        if enforcement == ENFORCEMENT_BLOCKED:
            blocked.append((item.field, report))

    if blocked:
        raise _blocked_error(
            blocked, side=side, app_name=app_name, entrypoint=entrypoint
        )


def _blocked_error(
    blocked: list[tuple[str, ArtifactValidationReport]],
    *,
    side: str,
    app_name: str,
    entrypoint: str,
) -> ArtifactValidationBlockedError:
    """Build the attributable failure for one or more blocked references.

    The first blocked reference is the primary — its declared-vs-found detail goes
    on the typed fields, so the red activity pane names a field and a disagreement
    rather than a count. Any others are counted in the message, so a task that
    broke five artifacts does not read as having broken one.

    ``location`` carries the side alongside the field because the two sides fail
    for opposite reasons and want opposite fixes: at ``handoff`` this task wrote
    the artifact, at ``ingest`` it was handed one.
    """
    field, report = blocked[0]
    label = field or "<unnamed reference>"
    extra = len(blocked) - 1
    others = (
        f" (and {extra} more blocked reference{'s' if extra > 1 else ''} in the "
        f"same {side} payload)"
        if extra
        else ""
    )
    return ArtifactValidationBlockedError(
        message=(
            f"Artifact validation blocked the {side} of contract field "
            f"'{label}': {report.reason or report.outcome}{others}"
        ),
        app_name=app_name or None,
        retryable=False,
        expectation=(
            f"an artifact matching the declaration for field '{label}'"
            + (f" on entrypoint '{entrypoint}'" if entrypoint else "")
        ),
        observed=report.format_report(),
        location=f"{side} payload, contract field '{label}'",
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
    """Drop references that would emit an identical row for an identical scan.

    Identity is ``(owner, field, artifact)``, where the artifact is the local path
    if there is one, the storage path otherwise, and — when a reference names
    neither — the reference object itself.

    **The fallback to object identity is what keeps the denominator honest.** A
    durable, lazy or freshly-constructed reference has no ``local_path`` at all, so
    keying on it alone would collapse every such reference under one field into a
    single row: a ``list[FileReference]`` of ten distinct durable artifacts would
    report once, understating the denominator FND-694's graduation review reads and
    hiding nine hand-offs that emitted nothing. Deduplicating is only ever allowed
    to drop a row that is genuinely a repeat of one already emitted; anything it
    cannot prove is a repeat has to be its own row.
    """
    seen: set[tuple[type | None, str, str | int]] = set()
    for item in named:
        ref = item.ref
        artifact = ref.local_path or ref.storage_path or id(ref)
        key = (item.owner, item.field, artifact)
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
    resolved anyway to keep the three facts apart:

    * "this artifact has no declaration" is ``not_declared``, a finding only on a
      boundary. Collapsing it into ``absent`` would report an app that declared its
      artifact correctly as having declared nothing.
    * "it is declared and there was no local copy to read" is ``absent``, and under
      a hard posture it blocks — the app asked for a check it could not be given.
    * "it is declared and the SDK could not read the *declaration*" is ``absent``
      too, but ``validator_broken``, so it never blocks.

    That last split is this function's twin of the wrapper's own
    ``ArtifactDeclarationError`` branch, and it has to agree with it: the same
    unreadable ``artifact_schemas.json`` reaches whichever of the two the reference
    happens to route through, and one of them failing a hard-mode activity for it
    while the other proceeds would make blocking depend on whether the artifact had
    been materialised. A malformed generated file is the SDK's read failing, not
    evidence about the artifact.
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
            validator_broken=True,
        )
    if declaration is None:
        return ArtifactValidationReport.not_declared(boundary=boundary)
    # Not `validator_broken`: nothing on our side failed. The app declared this
    # artifact and the hand-off could not be proved against it, which is exactly
    # what a hard posture exists to catch.
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
    mode: str = "",
    enforcement: str = "",
) -> None:
    """Emit the one queryable row, plus a readable WARNING when it needs one.

    Wrapped end to end: a defect in the emit path — a matrix that will not encode,
    an adapter that rejects a kwarg — must not be able to break the hand-off the
    row was describing. That includes the blocking path: the raise happens in the
    caller, after this returns, so an emit that falls over cannot turn a hard-mode
    block into a silent pass or an unattributable crash.

    The prose WARNING stays keyed on ``report.ok`` — a real finding against the
    artifact — and on an actual block, deliberately *not* on ``would_block``. Most
    of the fleet has declared nothing yet, so warning on every ``would_block``
    would put a line in every task's log for a state the app has not been asked to
    fix. The event row carries it, and the event row is what FND-694 reads.
    """
    try:
        # conformance: ignore[L018] app_name/entrypoint are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion, and a pinned outcome event exists precisely so its attributes are queryable columns
        logger.info(
            ARTIFACT_VALIDATION_EVENT,
            app_name=app_name,
            entrypoint=entrypoint,
            **artifact_validation_event_fields(
                report,
                artifact_field=artifact_field,
                side=side,
                mode=mode,
                enforcement=enforcement,
            ),
        )
        if enforcement == ENFORCEMENT_BLOCKED:
            logger.warning(
                "Artifact validation BLOCKED the %s hand-off of '%s' — this app "
                "declares artifact_validation_mode='hard', so the activity fails: "
                "%s",
                side,
                artifact_field or "<unnamed reference>",
                report.format_report(),
            )
        elif not report.ok:
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
