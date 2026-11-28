"""The bodies of ``App``'s framework storage tasks, and the pre-upload validation hook.

``App.upload``, ``download``, ``verify_refs``, ``upload_refs``,
``cleanup_files`` and ``cleanup_storage`` stay on ``App`` as the ``@task``
methods apps call (and the docstrings they read); each delegates here. Nothing
in this module imports ``application_sdk.app``: the app instance, and for the
cleanup tasks the run's task state, are passed in.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import (
    TYPE_CHECKING,
    Sequence,
    TypeVar,
)

from application_sdk._context_errors import (
    ObjectStoreNotConfiguredError,
    UpstreamObjectStoreNotConfiguredError,
)
from application_sdk._runtime.offload import run_best_effort, run_in_thread
from application_sdk.constants import (
    ASSET_VALIDATION_MAX_ITEMS_PER_AXIS,
    _run_prefix,
)
from application_sdk.contracts.cleanup import (
    CleanupInput,
    CleanupOutput,
    StorageCleanupInput,
    StorageCleanupOutput,
)
from application_sdk.contracts.storage import (
    DownloadInput,
    DownloadOutput,
    UploadInput,
    UploadOutput,
    UploadRefsInput,
    UploadRefsOutput,
    VerifyRefsInput,
    VerifyRefsOutput,
)
from application_sdk.contracts.types import FileReference, StorageTier, StoreTarget
from application_sdk.errors.leaves import InternalError as _InternalError
from application_sdk.execution._temporal._activity_errors import RunTempDirEscapeError
from application_sdk.execution._temporal.run_scope import (
    _current_run_key,
    _remove_run_credential_dirs,
)

# Fixed event name for the structured, queryable transformed-asset validation
# outcome. Emitted on every upload from inside the upload activity, so the
# Temporal activity context (workflow_id/run_id/app_name) is auto-stamped and the
# row joins to the workflow outcome in ClickHouse — mirrors the preflight gate's
# outcome event. Scalar counts land as top-level LogAttributes; the per-failure
# drill-down rides in the ``asset_validation_matrix`` JSON attribute. The name now
# lives in the shared event-name registry alongside the preflight gate's two and
# the generic artifact-validation one; its value is **unchanged** by that move,
# because shipped dashboards and alert rules key off the exact string.
from application_sdk.observability.events import ASSET_VALIDATION_EVENT
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from obstore.store import ObjectStore

    from application_sdk.app.base import App, TaskStateAccessor
    from application_sdk.validation.asset_rules import AssetCreationRules


__all__: list[str] = []

logger = get_logger(__name__)


# Cap on how many failure/orphan rows the event's drill-down matrix carries, and how
# many the human-readable ``format_report()`` lists, from the single source of truth
# in ``constants`` so the two can never drift. The event's scalar counts always
# reflect the full batch; the matrix is a bounded drill-down sample so a pathological
# batch can't produce an unbounded LogAttributes value. Passed explicitly to
# ``format_report(max_items=...)`` below to make the shared cap obvious at the call
# site.
#
# The matrix builder itself, and the whole event attribute map, moved to
# ``validation.assets`` with the fold-in (FND-690): the event and the check that
# feeds it now live in one module. The emitted row is unchanged by that move.
_VALIDATION_MATRIX_MAX_ROWS = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS


def _resolve_transformed_target(local_path: str) -> "Path | None":
    """Locate the ``transformed/`` asset subtree under ``local_path``, if any.

    Returns the path to validate, or ``None`` when there is nothing to validate
    (``local_path`` is not a ``transformed/`` asset subtree — e.g. a raw upload).
    """
    if not local_path:
        return None
    root = Path(local_path)
    if root.is_dir():
        transformed = root / "transformed"
        if transformed.is_dir():
            return transformed
        if "transformed" in root.parts:
            return root
        return None
    if "transformed" in root.parts:
        return root
    return None


async def _warn_on_invalid_transformed_assets(
    local_path: str | Sequence[str],
    app_name: str,
    *,
    not_local: Sequence[str] = (),
    creation_rules: "frozenset[AssetCreationRules]" = frozenset(),
) -> None:
    """Best-effort, warn-only validation of transformed asset NDJSON before upload.

    BLDX-1555 defense-in-depth at the SDR→Atlan boundary. When ``local_path``
    holds transformed asset output, every record is validated against the
    pyatlan_v9 ``.validate()`` backbone (plus the referential/orphan pass). On
    **every** upload a structured :data:`ASSET_VALIDATION_EVENT` is emitted with
    the per-axis counts, a compact per-failure ``asset_validation_matrix`` JSON
    attribute (a bounded sample) and an ``asset_validation_summary`` JSON attribute
    (complete per-type, per-rule counts), all allowlisted for OTLP so they reach ClickHouse and join to the
    workflow outcome by Temporal run id (mirrors the preflight gate's outcome
    event). A ``clean`` outcome is emitted too, so there is a denominator to rank
    flag-rate against. A human-readable WARNING with the full ``format_report()``
    is additionally logged only when the batch is flagged. Extracts and transforms
    are full by design by default, so the batch — the whole hand-off, see below —
    is complete and the orphan pass is accurate. This **never** blocks the upload
    and **never** raises — a defect in the scaffold must not break a real handoff
    — and it scans every record (no sampling) so the summary is accurate.

    **The check is reached through the generic wrapper** (ADR-0020, FND-690):
    ``validate_artifact(target, ModelSource(model=Asset))`` is the NDJSON x
    ``ModelSource`` cell, and that cell *is*
    :func:`~application_sdk.validation.assets.validate_transformed_dir` — same walk,
    same decode, same referential pass. What the wrapper adds is a second, generic
    outcome vocabulary over the same findings, and a guarantee this hook used to
    provide itself: every path through it resolves to a report rather than an
    exception.

    ``ASSET_VALIDATION_EVENT`` is emitted exactly as before. Its name and every
    attribute key are a shipped contract — dashboards, the
    ``asset_validation_matrix`` attribute and alert rules key off those exact
    strings — so the row is built by
    :func:`~application_sdk.validation.assets.asset_validation_event_fields` from the
    asset-vocabulary report the cell carries, i.e. from the same object this hook
    emitted from before the fold-in. This hook does **not** additionally emit
    ``ARTIFACT_VALIDATION_EVENT``: one hand-off, one row.

    **Only a bounded outcome comes back from the scan** (FND-4088). The full
    report is unbounded — every failure, twice — so the event's attribute map and
    the capped ``format_report()`` text are built inside the isolated child by
    :func:`~application_sdk.validation.assets.summarize_asset_validation`, and only
    that :class:`~application_sdk.validation.assets.AssetValidationOutcome` is
    pickled back. A batch that fails on every record costs this worker the cap,
    not the batch.

    The scan runs via
    :func:`application_sdk._runtime.offload.run_best_effort`, for two reasons.
    It keeps the event loop and the activity's auto-heartbeat free while a large
    batch is validated (ADR-0010). More importantly, it makes the never-raises
    contract hold even against *native* faults: the decode exercises third-party
    C extensions (msgspec via pyatlan_v9), and a segfault there is not a Python
    exception — in-process it would kill the whole Temporal worker (as one did,
    CNCT-85). ``run_best_effort`` isolates the scan in a child process, so a
    crash kills only the child; it then logs the warning and the handoff
    proceeds. The timeout bounds the scan for the same reason — a hung
    validation must not stall an upload forever. The wrapper is synchronous by
    design precisely so this caller owns that decision.

    **The batch is the whole hand-off, not one file** (FND-3414). The orphan pass
    is only accurate over everything delivered together: a fanned-out connector
    writes one file per typename, so a child's parent is routinely in a sibling
    file. ``local_path`` is therefore either one path (``upload``) or every
    declared part of a fan-in delivery (``upload_refs``), validated as one batch
    with one :data:`ASSET_VALIDATION_EVENT`. One part goes through the wrapper as
    above; several go straight to
    :func:`~application_sdk.validation.assets.validate_assets_as_artifact` — the
    cell the wrapper dispatches that one part to — because the wrapper's unit is
    a single artifact path and the parts share no local root to hand it.

    A declared part that is not on this pod (a distributed fan-in, where the
    upload streams it from the deployment store) makes the batch incomplete.
    ``upload_refs`` passes those parts' store keys as ``not_local``. When any of
    them is a transformed part, the orphan pass is skipped with a warning, and
    per-asset validation still runs over the parts that are local — reporting a
    parent as missing because it was written on another pod would be the false
    positive this fixes. When *no* transformed part is local, the same warning
    says validation was skipped entirely, so a fully distributed fan-in is never
    silent.

    ``creation_rules`` (the app's :attr:`App.asset_validation_rules`) adds the
    opt-in create-time checks pyatlan no longer runs. The wrapper's
    ``ModelSource`` carries no asset-specific options, so a non-empty rule set
    goes straight to the cell, as a multi-part batch already does.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — deferred-constant import mirrors upload()'s pattern
        VALIDATE_ASSETS_ON_UPLOAD,
        VALIDATE_ASSETS_TIMEOUT_SECONDS,
    )

    # Cheap checks stay in-parent so a disabled feature or a raw upload never
    # pays for a child-process dispatch.
    if not VALIDATE_ASSETS_ON_UPLOAD:
        return
    paths = [local_path] if isinstance(local_path, str) else list(local_path)
    targets = [
        str(target)
        for target in map(_resolve_transformed_target, paths)
        if target is not None
    ]
    remote = [key for key in not_local if "transformed" in PurePosixPath(key).parts]
    if remote:
        logger.warning(
            "Transformed-asset validation: %d of %d declared transformed file(s) "
            "are not on this pod, so the batch is incomplete; %s",
            len(remote),
            len(remote) + len(targets),
            (
                "skipping the referential (orphan) check"
                if targets
                else "skipping validation"
            ),
        )
    if not targets:
        return
    check_referential_integrity = not remote

    from pyatlan_v9.model.assets import (  # noqa: PLC0415 — deferred: pyatlan_v9 stays off the import path of an app that never uploads transformed assets
        Asset,
    )

    from application_sdk.validation import (  # noqa: PLC0415 — deferred: only load the validator on the upload path
        AssetBatchScope,
        ModelSource,
        summarize_asset_validation,
        validate_artifact,
        validate_assets_as_artifact,
    )

    # One path is ``upload``; a list is every declared part of an ``upload_refs``
    # fan-in (see the docstring).
    scope = AssetBatchScope(
        upload_kind="upload" if isinstance(local_path, str) else "upload_refs",
        parts_validated=len(targets),
        parts_not_local=len(remote),
    )
    # Best-effort: run_best_effort isolates the scan in a child process and, on
    # any native crash / timeout / error, logs a warning and returns None — the
    # upload is never blocked, failed, or crashed by the validation scaffold.
    # summarize_asset_validation builds the event and the capped report text in
    # the child, so only that bounded outcome is pickled back into this worker,
    # never the full report (FND-4088).
    if len(targets) == 1 and check_referential_integrity and not creation_rules:
        outcome = await run_best_effort(
            summarize_asset_validation,
            validate_artifact,
            targets[0],
            ModelSource(model=Asset),
            app_name=app_name,
            scope=scope,
            max_items=_VALIDATION_MATRIX_MAX_ROWS,
            label="Transformed-asset validation",
            logger=logger,
            timeout=VALIDATE_ASSETS_TIMEOUT_SECONDS,
        )
    else:
        outcome = await run_best_effort(
            summarize_asset_validation,
            validate_assets_as_artifact,
            targets,
            app_name=app_name,
            scope=scope,
            max_items=_VALIDATION_MATRIX_MAX_ROWS,
            check_referential_integrity=check_referential_integrity,
            creation_rules=creation_rules,
            label="Transformed-asset validation",
            logger=logger,
            timeout=VALIDATE_ASSETS_TIMEOUT_SECONDS,
        )

    if outcome is None:
        # run_best_effort swallowed a native crash / timeout / error (it already
        # logged a warning). The "nothing to validate" cases (flag off, not a
        # transformed/ subtree) already returned above, so reaching here with
        # None means the scan itself failed — no counts to emit.
        return

    if outcome.event_fields is None:
        # The wrapper resolved to an outcome without running the cell — it never
        # raises, so a broken plug-in or an unreadable artifact arrives as a plain
        # report instead of an exception. There are no asset-vocabulary counts to
        # project, and inventing zeros would put a fabricated denominator on the
        # board. The wrapper has already logged the cause with a traceback; name the
        # skip so the gap in the event stream is explicable.
        logger.warning(
            "Transformed-asset validation produced no counts (%s: %s); "
            "handoff continues",
            outcome.outcome,
            outcome.reason or "no reason recorded",
        )
        return

    # Emit the structured outcome on every validated upload (clean + flagged) so
    # the row is queryable and gives a denominator. Wrapped: a defect in the
    # emit path must never break a real handoff.
    try:
        logger.info(ASSET_VALIDATION_EVENT, **outcome.event_fields)
        if outcome.flagged:
            logger.warning(
                "Transformed-asset validation flagged issues before upload "
                "(handoff continues): %s",
                outcome.flagged_report,
            )
    except Exception:  # defense-in-depth must never break the upload
        logger.warning(
            "Transformed-asset validation outcome emission failed (handoff continues)",
            exc_info=True,
        )


_TransferOutput = TypeVar("_TransferOutput", UploadOutput, DownloadOutput)


def _tag_ref_store(
    out: _TransferOutput,
    store: "ObjectStore",
    deployment: "ObjectStore | None",
) -> _TransferOutput:
    """Record on *out*'s ref which store its bytes are in.

    ``App.upload`` / ``App.download`` route to the upstream store when one is
    bound, but the interceptor materialises a ref into the next task from the
    deployment store unless the ref says otherwise. In a two-store deployment
    those are different buckets: the downstream task lists an empty prefix and
    fails AAF-STR-009 (FND-3795). Compared by identity, the same test
    ``single_store`` uses — the in-cluster wiring aliases one store under both
    names, and that stays ``DEPLOYMENT``.
    """
    if store is deployment or not out.ref.is_durable:
        return out
    return out.model_copy(
        update={"ref": out.ref.model_copy(update={"store": StoreTarget.UPSTREAM})}
    )


def _cleanup_ref_key(ref: FileReference) -> tuple[object, ...]:
    """Identity of the stored object(s) a ref names, for cleanup dedup."""
    return (ref.storage_path, ref.store, ref.tier, ref.manifest_path)


def _merge_cleanup_refs(
    tracked: Iterable[FileReference], carried: Iterable[FileReference]
) -> list[FileReference]:
    """Combine this worker's tracked refs with the workflow's carried ones.

    Keeps one ref per stored object, so a key reaching ``cleanup_storage`` by
    both routes is deleted and counted once. Tracked refs come first.
    """
    merged: dict[tuple[object, ...], FileReference] = {}
    for ref in (*tracked, *carried):
        merged.setdefault(_cleanup_ref_key(ref), ref)
    return list(merged.values())


def _upload_targets(
    deployment: ObjectStore | None,
    upstream: ObjectStore | None,
    *,
    dual_write: bool,
    dual_write_required: bool,
) -> list[tuple[ObjectStore, str, bool]]:
    """The ordered ``(store, label, fatal)`` targets one upload writes to.

    See ADR-0014 §"BLDX-1464 dual-write" for the full routing decision.

    Raises:
        ObjectStoreNotConfiguredError: Neither store is bound.
    """
    if (
        dual_write
        and upstream is not None
        and deployment is not None
        and upstream is not deployment
    ):
        return [
            (deployment, "deployment", dual_write_required),
            (upstream, "upstream", True),
        ]
    single = upstream or deployment
    if single is None:
        raise ObjectStoreNotConfiguredError()
    return [(single, "upstream" if single is upstream else "deployment", True)]


async def upload(
    app: App,
    input: UploadInput,
    *,
    validate_assets: bool = True,
) -> UploadOutput:
    """Body of :meth:`App.upload` (``App._upload_impl``); see its docstring."""
    from application_sdk.constants import (  # noqa: PLC0415 — import here to avoid module-level circular import (same pattern as normalize_key)
        DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED,
        DEPLOYMENT_ARTIFACT_DUAL_WRITE_REQUIRED,
        ENABLE_ATLAN_UPLOAD,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: app.base is imported by execution which imports storage
        normalize_key,
    )
    from application_sdk.storage.transfer import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        upload as _upload,
    )

    deployment = app.context.storage
    upstream = app.context.upstream_storage

    # BLDX-1619: a deployment that set ENABLE_ATLAN_UPLOAD expects artifacts
    # in Atlan's bucket. Falling back to the deployment store here returns a
    # positive file count for a write publish will never see, so fail before
    # doing any work rather than after.
    if ENABLE_ATLAN_UPLOAD and upstream is None:
        raise UpstreamObjectStoreNotConfiguredError()

    # BLDX-1555 defense-in-depth: validate transformed assets against the
    # pyatlan_v9 backbone before the handoff. Warn-only, best-effort, and
    # run in an isolated child process (CNCT-85) so a native decode fault
    # kills only the child and never blocks or crashes the event loop.
    if validate_assets:
        await _warn_on_invalid_transformed_assets(
            input.local_path,
            app._app_name,
            creation_rules=app.asset_validation_rules,
        )

    targets = _upload_targets(
        deployment,
        upstream,
        dual_write=DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED,
        dual_write_required=DEPLOYMENT_ARTIFACT_DUAL_WRITE_REQUIRED,
    )

    run_prefix = _run_prefix(app._app_name, app.context.workflow_id, app.context.run_id)
    app_prefix = input.tier.upload_prefix(run_prefix=run_prefix, app_name=app._app_name)

    # Derive the FileReference for cross-store dedup / deployment-store fallback.
    # normalize_key strips TEMPORARY_PATH → canonical deployment-store key.
    # Guard: "" means local_path resolved to the store root — skip source_ref.
    _store_key = normalize_key(input.local_path) if input.local_path else ""
    source_ref = input.ref or (
        FileReference(local_path=input.local_path, storage_path=_store_key)
        if _store_key
        else None
    )

    # Fan-out: iterate targets in order. Fatal failures are deferred until all
    # remaining targets complete so the upstream write always runs and a copy
    # lands somewhere. If both writes fail the deployment error is chained as
    # __cause__ of the upstream error so neither traceback is lost.
    result: UploadOutput | None = None
    deferred_required_error: Exception | None = None
    for target_store, label, fatal in targets:
        # FND-536: every leg — including the deployment leg — gets the
        # deployment store as its fallback source. Withholding it there made
        # a deployment→deployment copy inexpressible: with ``local_path``
        # absent (cross-pod / ref-only, the shape the P042 bridges use)
        # ``transfer.upload`` could not take its deployment-store fallback
        # branch and raised "local_path does not exist", which is a spurious
        # WARNING under ``best_effort`` and fails the run under ``required``.
        # Passing it costs nothing on the local-present paths: the directory
        # reconcile is gated on a *distinct* source store, the single-file
        # path never reads it, and ``_upload_from_store`` short-circuits a
        # same-store copy of an identical key without any sidecar GET.
        source_store = app.context.storage
        try:
            out = await _upload(
                input.local_path,
                input.storage_path,
                storage_subdir=input.storage_subdir,
                skip_if_exists=input.skip_if_exists,
                raise_on_empty=input.raise_on_empty,
                store=target_store,
                _source_ref=source_ref,
                _source_store=source_store,
                _app_prefix=app_prefix,
                _tier=input.tier,
                _output_path=run_prefix,
            )
        except Exception as exc:
            if fatal:
                logger.error(
                    "Object-store upload to %s store failed for prefix %s; "
                    "will fail run after remaining targets complete",
                    label,
                    app_prefix,
                    exc_info=True,
                )
                if deferred_required_error is not None:
                    # Both writes failed: chain the earlier error as __cause__ so
                    # neither traceback is lost when the exception is surfaced.
                    exc.__cause__ = deferred_required_error
                deferred_required_error = exc
            else:
                logger.warning(
                    "Object-store upload to %s store failed for prefix %s "
                    "(non-fatal); continuing to next target",
                    label,
                    app_prefix,
                    exc_info=True,
                )
            continue
        result = _tag_ref_store(
            out, target_store, deployment
        )  # last successful write is authoritative (upstream in SDR)

    if deferred_required_error is not None:
        raise deferred_required_error
    if result is None:
        # Unreachable under current target-construction logic (the single-target
        # branch is always fatal=True), but guards against future changes.
        raise _InternalError(
            message="App.upload fan-out captured no result — this is a programming error",
            invariant="upload fan-out must produce at least one result",
        )
    return result


async def download(
    app: App,
    input: DownloadInput,
) -> DownloadOutput:
    """Body of :meth:`App.download`; see its docstring."""
    from application_sdk.storage.transfer import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        download as _download,
    )

    store = app.context.upstream_storage or app.context.storage
    if store is None:
        raise ObjectStoreNotConfiguredError()
    # Resolve storage_path: explicit field takes precedence over ref.storage_path
    storage_path = input.storage_path
    if not storage_path and input.ref is not None:
        storage_path = input.ref.storage_path or ""

    # A ref this run produced keeps its tier, so its TRANSIENT output is
    # still cleaned up; any other key is not the run's to delete (FND-3815).
    tier = input.tier
    if tier is None:
        ref = input.ref
        from_ref = ref is not None and storage_path == ref.storage_path
        tier = ref.tier if ref is not None and from_ref else StorageTier.RETAINED
    out = await _download(
        storage_path,
        input.local_path,
        skip_if_exists=input.skip_if_exists,
        store=store,
        tier=tier,
    )
    return _tag_ref_store(out, store, app.context.storage)


async def _missing_for_ref(
    ref: FileReference,
    store: ObjectStore,
    parent_listings: dict[str, dict[str, tuple[int, str | None]]],
) -> str | None:
    """Why one declared ref fails :func:`verify_refs`, or ``None`` if it is all there.

    *ref* has a ``storage_path`` inside the hand-off prefix; the caller checks both.
    """
    from application_sdk.storage.batch import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        list_data_keys,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        _exists_confirmed,
    )
    from application_sdk.storage.reference import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        _fetch_manifest,
    )

    raw_key = ref.storage_path or ""
    key = raw_key.strip("/")
    # Directory or single file? Two independent signals, because
    # neither alone is sufficient:
    #
    # * The trailing slash is what ``persist_file_reference``'s
    #   directory branch writes (``_make_storage_prefix`` always ends
    #   in ``/``), and it survives a directory holding exactly one
    #   file — which ``file_count`` does not.
    # * ``file_count`` catches a directory ref whose key was pinned by
    #   hand without the slash.
    #
    # Getting this wrong is not a near-miss: a one-file directory ref
    # HEADed as an object key 404s on a prefix that is perfectly
    # intact, so the run fails claiming data is lost when it is not.
    if ref.manifest_path is not None:
        # The producer wrote the manifest last, so it names exactly the
        # objects the hand-off covers; stray objects under a reused
        # prefix cannot pad the count.
        manifest = await _fetch_manifest(store, ref.manifest_path)
        if manifest is None:
            return f"{ref.manifest_path} (manifest)"
        listed = len(manifest.files)
        if listed < max(ref.file_count, 1):
            return f"{key}/ (manifest lists {listed}/{ref.file_count} files)"
        # The manifest proves every object landed when it was written,
        # not that they are still there. One listing answers that for
        # every entry; it is a subset check, so stray objects under a
        # reused prefix still cannot stand in for a missing one.
        present = set(await list_data_keys(key + "/", store=store, normalize=False))
        absent = [e.path for e in manifest.files if f"{key}/{e.path}" not in present]
        if absent:
            return f"{key}/ ({listed - len(absent)}/{listed} manifest objects)"
    elif raw_key.endswith("/") or ref.file_count != 1:
        if ref.file_count < 1:
            # ``len(found) < 0`` is false for every listing, so a
            # zero-count ref would otherwise pass without checking
            # anything — a vacuous pass is indistinguishable from a
            # real one, which is the failure mode this task exists to
            # remove. A producer declaring zero files has declared
            # nothing to verify.
            return f"{key}/ (declared 0 files)"
        # ``list_data_keys``, not ``list_keys``: every uploaded object
        # carries a ``{key}.sha256`` sidecar, so a raw listing returns
        # roughly 2N keys for N files and a tree missing half its data
        # objects would still clear ``file_count``.
        #
        # The trailing slash is added explicitly. ``list_keys`` only
        # appends one when ``normalize=True``, and this call passes
        # ``normalize=False`` to match the key the writer used — so
        # without it the listing would also match sibling prefixes
        # (``transformed/table`` picking up ``transformed/table_v2/``)
        # and count their objects toward this ref.
        found = await list_data_keys(key + "/", store=store, normalize=False)
        if len(found) < ref.file_count:
            return f"{key}/ ({len(found)}/{ref.file_count} objects)"
    elif not await _exists_confirmed(
        key, store=store, normalize=False, listing_cache=parent_listings
    ):
        # Confirmed, not a bare HEAD: a gateway replica without routes
        # answers HEAD with a bodiless 404 for an existing object, so a
        # miss is checked against a (cached) listing of the parent.
        return key
    return None


async def verify_refs(
    app: App,
    input: VerifyRefsInput,
) -> VerifyRefsOutput:
    """Body of :meth:`App.verify_refs` (``App._verify_refs_impl``); see its docstring."""
    from application_sdk._runtime.progress import (  # noqa: PLC0415 — circular: _runtime imports app contracts
        current_progress_tracker,
    )
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules that import app.base
        StorageHandoffIncompleteError,
    )

    if input.store is StoreTarget.UPSTREAM:
        store = app.context.upstream_storage or app.context.storage
    else:
        store = app.context.storage
    if store is None:
        raise ObjectStoreNotConfiguredError()

    prefix = input.prefix.strip("/")
    missing: list[str] = []
    outside: list[str] = []
    verified_file_count = 0
    # Parent listings taken to confirm a HEAD miss, keyed by listing
    # prefix. Sibling refs share a parent, so during a gateway outage that
    # fails many HEADs one listing answers all of them instead of
    # re-enumerating the same folder once per ref.
    parent_listings: dict[str, dict[str, tuple[int, str | None]]] = {}

    for ref in input.refs:
        raw_key = ref.storage_path or ""
        key = raw_key.strip("/")
        if not key:
            # A producer that cannot say where it wrote has declared
            # nothing — that is the hole, not a reason to skip the check.
            missing.append("<no storage_path>")
            continue
        if prefix and not (key == prefix or key.startswith(prefix + "/")):
            outside.append(key)
            continue

        problem = await _missing_for_ref(ref, store, parent_listings)
        if problem is not None:
            missing.append(problem)
            continue
        verified_file_count += ref.file_count
        # One store round-trip completed. A file boundary, not a record
        # boundary (ADR-0018) — and without it this loop is a run of bare
        # awaits with no progress signal, so a wedged HEAD against an
        # unreachable store would hold silently to the backstop instead of
        # being caught by the watchdog in minutes.
        current_progress_tracker().mark_progress("storage.verify_ref")

    if missing or outside:
        raise StorageHandoffIncompleteError(
            "Declared outputs are not all present in the object store: "
            f"{len(missing)} missing, {len(outside)} outside the handoff "
            f"prefix, out of {len(input.refs)} declared",
            missing_keys=missing,
            outside_prefix_keys=outside,
            prefix=prefix or None,
            declared_count=len(input.refs),
        )

    logger.info(
        "Verified %d declared file reference(s) (%d object(s)) under prefix %s",
        len(input.refs),
        verified_file_count,
        prefix or "<any>",
    )
    return VerifyRefsOutput(
        verified_count=len(input.refs),
        verified_file_count=verified_file_count,
        prefix=prefix,
    )


async def upload_refs(
    app: App,
    input: UploadRefsInput,
) -> UploadRefsOutput:
    """Body of :meth:`App.upload_refs`; see its docstring."""
    prefix = input.prefix.strip("/")
    if not input.files:
        logger.warning(
            "upload_refs called with an empty declaration; returning an empty "
            "prefix instead of %s, which names an empty tree",
            prefix or "<run prefix>",
        )
        return UploadRefsOutput()

    source_prefix = input.source_prefix.strip("/")

    # Validate the declaration as one batch, not per file (FND-3414): a
    # fanned-out connector writes one file per typename, so a child's parent
    # is usually in a sibling file and a per-file orphan pass flags nearly
    # every cross-typename reference. A part this pod never held is passed
    # by store key, so the hook can say the batch is incomplete.
    local: list[str] = []
    not_local: list[str] = []
    for declared in input.files:
        path = declared.ref.local_path
        if path and Path(path).exists():
            local.append(path)
        else:
            not_local.append(declared.ref.storage_path or "")
    await _warn_on_invalid_transformed_assets(
        local,
        app._app_name,
        not_local=not_local,
        creation_rules=app.asset_validation_rules,
    )

    delivered: list[FileReference] = []
    file_count = 0
    for declared in input.files:
        source_key = (declared.ref.storage_path or "").strip("/")
        leaf = declared.label
        if not leaf and source_prefix and source_key.startswith(source_prefix + "/"):
            leaf = source_key[len(source_prefix) + 1 :].strip("/")
        if not leaf:
            from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules that import app.base
                UnplaceableDeclaredFileError,
            )

            raise UnplaceableDeclaredFileError(
                "upload_refs cannot place a declared file: it carries no "
                f"label and its storage_path {source_key!r} does not sit "
                f"under source_prefix {source_prefix!r}. Set one or the "
                "other — the SDK does not guess a leaf, because a guess "
                "that works for four entities and flattens one is worse "
                "than an error",
                storage_path=source_key,
                source_prefix=source_prefix,
            )
        out = await app._upload_impl(
            UploadInput(
                ref=declared.ref,
                local_path=declared.ref.local_path or "",
                storage_path=f"{prefix}/{leaf}" if prefix else leaf,
                tier=input.tier,
                # A declared file that contributes zero objects is a hole in
                # the tree the consumer will walk — fail here, loudly.
                raise_on_empty=True,
            ),
            validate_assets=False,
        )
        delivered.append(out.ref)
        file_count += out.ref.file_count

    if input.verify:
        await app._verify_refs_impl(
            VerifyRefsInput(
                refs=[
                    r.model_copy(update={"auto_materialize": False}) for r in delivered
                ],
                prefix=prefix,
                store=StoreTarget.UPSTREAM,
            )
        )

    logger.info(
        "Delivered %d declared file(s) (%d object(s)) to prefix %s",
        len(delivered),
        file_count,
        prefix or "<store root>",
    )
    return UploadRefsOutput(prefix=prefix, refs=delivered, file_count=file_count)


async def _remove_path(path: str) -> None:
    """Delete ``path`` (file or directory) off the event loop.

    Offloaded because ``rmtree``/``remove`` over a large tracked
    directory is syscall-bound and can run long enough to starve the
    auto-heartbeat if it ran directly on the event loop.
    """
    if os.path.isdir(path):
        await run_in_thread(shutil.rmtree, path)
    else:
        await run_in_thread(os.remove, path)


async def _remove_local(path: str, path_results: dict[str, bool], what: str) -> None:
    """Remove *path* if it exists and record the outcome; never raises."""
    try:
        if os.path.exists(path):
            await _remove_path(path)
        path_results[path] = True
    except Exception:
        logger.warning("Failed to delete %s during cleanup", what, exc_info=True)
        path_results[path] = False


def _cleanup_dir_paths(input: CleanupInput) -> list[str]:
    """The convention-based temp directories ``cleanup_files`` sweeps."""
    from application_sdk.constants import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        CLEANUP_BASE_PATHS,
    )
    from application_sdk.execution._temporal.activity_utils import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        run_temp_dir,
    )

    if input.extra_paths:
        return input.extra_paths
    if CLEANUP_BASE_PATHS:
        return CLEANUP_BASE_PATHS
    # The same directory a path-less FileReference materialises into
    # (storage/reference.py ``_default_materialize_dir``), so those
    # downloads go with the run instead of piling up on the pod.
    try:
        return [run_temp_dir()]
    except RunTempDirEscapeError:
        # A workflow ID with ``..`` segments: deleting what it points
        # at would reach outside TEMPORARY_PATH, so sweep nothing.
        logger.warning(
            "Skipping the run scratch directory sweep: it resolves "
            "outside TEMPORARY_PATH",
            exc_info=True,
        )
        return []


async def cleanup_files(
    app: App,
    input: CleanupInput,
    *,
    state: TaskStateAccessor,
) -> CleanupOutput:
    """Body of :meth:`App.cleanup_files`; see its docstring."""
    from application_sdk.constants import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        TRACKED_FILE_REFS_KEY,
    )

    path_results: dict[str, bool] = {}

    # 1. Delete tracked FileReference local paths (+ .sha256 sidecars).
    for ref in state.get(TRACKED_FILE_REFS_KEY) or ():
        if ref.local_path:
            for p in (ref.local_path, ref.local_path + ".sha256"):
                await _remove_local(p, path_results, "local path")

    # 2. Delete this run's credential file directories on this worker.
    #    Tracked separately because they hold secrets and live outside
    #    the run scratch dir, so no convention-based sweep below reaches
    #    them. Ones written on another worker are removed there, by its
    #    sweeper, once the run has closed.
    path_results.update(await _remove_run_credential_dirs(_current_run_key()))

    # 3. Delete convention-based temp directories.
    for base_path in _cleanup_dir_paths(input):
        await _remove_local(base_path, path_results, "temp directory")

    return CleanupOutput(path_results=path_results)


#: How many object-store deletes ``cleanup_storage`` runs at once.
_MAX_CONCURRENT_DELETES = 20


@dataclass
class _StorageSweep:
    """One ``cleanup_storage`` run: where it deletes, what it spares, what it counted.

    ``durable_keys`` / ``durable_prefixes`` are the keys a RETAINED/PERSISTENT
    ref in this run points at. A TRANSIENT ref can alias one of them (an app
    that hand-builds a TRANSIENT ref on a key ``upload_refs`` delivered as
    RETAINED), and deleting through the alias would destroy what the durable
    ref promises downstream. The durable tier wins: such keys are skipped,
    never deleted, by the tracked-ref pass.
    """

    store: ObjectStore
    resolved: ObjectStore
    protected: Sequence[str]
    sem: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(_MAX_CONCURRENT_DELETES)
    )
    durable_keys: set[str] = field(default_factory=set)
    durable_prefixes: list[str] = field(default_factory=list)
    deleted: int = 0
    skipped: int = 0
    errors: int = 0

    def note_durable(self, refs: Sequence[FileReference]) -> None:
        """Record the keys every non-TRANSIENT ref in *refs* points at."""
        for ref in refs:
            durable_path: str | None = getattr(ref, "storage_path", None)
            if not durable_path:
                continue
            if getattr(ref, "tier", StorageTier.TRANSIENT) == StorageTier.TRANSIENT:
                continue
            if durable_path.endswith("/"):
                self.durable_prefixes.append(durable_path)
            else:
                self.durable_keys.update((durable_path, durable_path + ".sha256"))
            durable_manifest: str | None = getattr(ref, "manifest_path", None)
            if durable_manifest:
                self.durable_keys.add(durable_manifest)

    def is_protected(self, key: str) -> bool:
        return any(key.startswith(p) for p in self.protected)

    def is_durable(self, key: str) -> bool:
        return key in self.durable_keys or any(
            key.startswith(p) for p in self.durable_prefixes
        )

    async def delete_one(self, key: str) -> bool:
        from application_sdk.storage.ops import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
            delete,
        )

        async with self.sem:
            try:
                await delete(key, self.store, normalize=False)
                return True
            except Exception:
                logger.warning(
                    "Object store delete failed during cleanup",
                    exc_info=True,
                )
                return False

    def count(self, ok: bool) -> None:
        if ok:
            self.deleted += 1
        else:
            self.errors += 1

    async def delete_listing(self, prefix: str, *, spare_durable: bool) -> int:
        """Delete every key under *prefix*; return how many durable aliases it spared."""
        import obstore as obs  # noqa: PLC0415 — lazy: heavy Rust ext; keep out of the workflow-sandbox import set

        aliased = 0
        # async for: each page is fetched off the loop. A plain
        # ``for`` drives the same ListStream synchronously, blocking
        # the loop on every page's round trip (FND-4084).
        async for batch in obs.list(self.resolved, prefix=prefix):
            tasks = []
            for item in batch:
                key = str(item["path"])
                if self.is_protected(key):
                    self.skipped += 1
                    continue
                if spare_durable and self.is_durable(key):
                    self.skipped += 1
                    aliased += 1
                    continue
                tasks.append(self.delete_one(key))
            for ok in await asyncio.gather(*tasks):
                self.count(ok)
        return aliased

    async def delete_dir_ref(self, ref: FileReference, storage_path: str) -> int:
        """Delete a directory ref's keys and its manifest; return aliases spared."""
        aliased = await self.delete_listing(storage_path, spare_durable=True)
        # The manifest sits outside the prefix by design, so the
        # listing above never reaches it.
        manifest_key: str | None = getattr(ref, "manifest_path", None)
        if manifest_key:
            if self.is_protected(manifest_key):
                self.skipped += 1
            elif self.is_durable(manifest_key):
                self.skipped += 1
                aliased += 1
            else:
                self.count(await self.delete_one(manifest_key))
        return aliased

    async def delete_file_ref(self, storage_path: str) -> int:
        """Delete a single-file ref's key and ``.sha256`` sidecar; return aliases spared."""
        aliased = 0
        for key in (storage_path, storage_path + ".sha256"):
            if self.is_durable(key):
                self.skipped += 1
                aliased += 1
                continue
            self.count(await self.delete_one(key))
        return aliased

    async def delete_tracked(self, ref: FileReference) -> None:
        """Delete one tracked ref's objects, if it is TRANSIENT and this store's."""
        storage_path: str | None = getattr(ref, "storage_path", None)
        if not storage_path:
            return
        # The ref names the upstream store; this task deletes only from
        # the deployment store, where the same key is not this ref's.
        if (
            self.is_protected(storage_path)
            or getattr(ref, "store", StoreTarget.DEPLOYMENT) == StoreTarget.UPSTREAM
            or getattr(ref, "tier", StorageTier.TRANSIENT) != StorageTier.TRANSIENT
        ):
            self.skipped += 1
            return
        if storage_path.endswith("/"):
            # Directory ref — stream-and-delete sub-keys.
            aliased = await self.delete_dir_ref(ref, storage_path)
        else:
            # Single file — delete key and .sha256 sidecar.
            aliased = await self.delete_file_ref(storage_path)
        if aliased:
            logger.warning(
                "Not deleting %d key(s) of TRANSIENT ref %s: a RETAINED or "
                "PERSISTENT ref in this run points at the same key(s). Track "
                "each key under a single tier",
                aliased,
                storage_path,
            )


async def cleanup_storage(
    app: App,
    input: StorageCleanupInput,
    *,
    state: TaskStateAccessor,
) -> StorageCleanupOutput:
    """Body of :meth:`App.cleanup_storage`; see its docstring."""
    from application_sdk.constants import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        PROTECTED_STORAGE_PREFIXES,
        TRACKED_FILE_REFS_KEY,
    )
    from application_sdk.execution import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
        build_output_path,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        _resolve_store,
    )

    store = app.context.storage if app._context is not None else None
    if store is None:
        return StorageCleanupOutput()

    sweep = _StorageSweep(
        store=store,
        resolved=_resolve_store(store),
        protected=PROTECTED_STORAGE_PREFIXES,
    )

    # 1. Delete tracked transient objects: the refs this worker tracked,
    # plus the refs the workflow read off every task's input and result
    # (``input.file_refs``), which cover the tasks that ran on other
    # workers. One entry per stored object, this worker's copy first.
    tracked_refs = _merge_cleanup_refs(
        state.get(TRACKED_FILE_REFS_KEY) or (), input.file_refs
    )
    sweep.note_durable(tracked_refs)
    for ref in tracked_refs:
        await sweep.delete_tracked(ref)

    # 2. Delete run-scoped prefix (opt-in).
    if input.include_prefix_cleanup:
        await sweep.delete_listing(build_output_path() + "/", spare_durable=False)

    return StorageCleanupOutput(
        deleted_count=sweep.deleted,
        skipped_count=sweep.skipped,
        error_count=sweep.errors,
    )
