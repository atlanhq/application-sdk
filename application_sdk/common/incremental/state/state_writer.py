"""Current state writer utilities for incremental extraction.

This module builds a run's current-state snapshot and commits it:

1. ``download_transformed_data()`` — get the current run's transformed output.
2. ``create_current_state_snapshot()`` — copy entities into a run-scoped
   current-state directory, diff it against the materialized previous state
   (with deletion detection), upload the diff, then commit the snapshot via
   :meth:`CurrentStateStore.commit` (upload, manifest last, prune).

Current-state is lightweight — it contains only what was extracted in the
current run. Publish-cache is the source of truth for complete state.

``prepare_previous_state``, ``prepare_current_state_directory``,
``upload_current_state`` and ``cleanup_previous_state`` are deprecated shims
from the fixed per-connection directory layout; use
:class:`~application_sdk.common.incremental.state.store.CurrentStateStore` and
:class:`~application_sdk.common.incremental.state.store.RunStateDirs` instead.
"""

import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from typing_extensions import deprecated

from application_sdk._runtime.offload import run_in_thread
from application_sdk.common.incremental.helpers import (
    copy_directory_parallel,
    count_json_files_recursive,
    get_persistent_artifacts_path,
)
from application_sdk.common.incremental.helpers import (
    get_persistent_s3_prefix as _get_persistent_s3_prefix,
)
from application_sdk.common.incremental.models import EntityType
from application_sdk.common.incremental.state.incremental_diff import (
    create_incremental_diff,
)
from application_sdk.common.incremental.state.store import (
    CurrentStateSnapshot,
    CurrentStateStore,
)
from application_sdk.common.incremental.state.table_scope import (
    close_scope,
    get_current_table_scope,
    get_scope_length,
    get_table_qns_from_columns,
)
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnectionManager,
)
from application_sdk.constants import (
    CURRENT_STATE_SUBPATH,
    INCREMENTAL_DIFF_SUBPATH_TEMPLATE,
    TRANSFORMED_SUBDIR,
)
from application_sdk.execution import get_object_store_prefix
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage._concurrency import _run_drained
from application_sdk.storage.batch import download_prefix, upload_prefix

logger = get_logger(__name__)


@dataclass
class CurrentStateResult:
    """Result of current state creation operation.

    Attributes:
        current_state_dir: Local path to current state directory
        current_state_s3_prefix: S3 prefix where state was uploaded
        total_files: Total JSON files in current state
        incremental_diff_dir: Local path to incremental diff (if created)
        incremental_diff_s3_prefix: S3 prefix for diff (if uploaded)
        incremental_diff_files: Number of files in diff (0 if not created)
        snapshot: The snapshot the commit produced
    """

    current_state_dir: Path
    current_state_s3_prefix: str
    total_files: int
    incremental_diff_dir: Path | None = None
    incremental_diff_s3_prefix: str | None = None
    incremental_diff_files: int = 0
    snapshot: CurrentStateSnapshot | None = None


async def download_transformed_data(output_path: str) -> Path:
    """Download transformed files from S3 to local storage.

    Downloads the current workflow run's transformed output from S3,
    which contains the freshly extracted and transformed metadata.

    Args:
        output_path: Local output path from workflow_args (e.g., ./local/tmp/wf-123/run-456)

    Returns:
        Path to local transformed directory

    Raises:
        FileNotFoundError: If output_path is empty or invalid

    Example:
        >>> transformed_dir = await download_transformed_data("./local/tmp/wf-123/run-456")
        >>> print(f"Transformed data in: {transformed_dir}")
    """
    output_path_str = str(output_path).strip()
    if not output_path_str:
        raise FileNotFoundError("No output_path provided in workflow_args")

    transformed_local_path = os.path.join(output_path_str, TRANSFORMED_SUBDIR)
    transformed_s3_prefix = get_object_store_prefix(transformed_local_path)

    logger.info("Downloading transformed files from S3: %s", transformed_s3_prefix)

    # Ensure local directory exists before download
    transformed_dir = Path(transformed_local_path)
    transformed_dir.mkdir(parents=True, exist_ok=True)

    # strip_prefix: transformed_dir already *is* the run's transformed directory,
    # so the store prefix must not be repeated inside it — downstream readers
    # (table_scope, column extraction) key off <transformed_dir>/table (FND-340).
    await download_prefix(
        prefix=transformed_s3_prefix,
        local_dir=str(transformed_dir),
        strip_prefix=True,
    )

    return transformed_dir


async def materialize_previous_state(
    store: CurrentStateStore,
    dest: Path,
    snapshot: CurrentStateSnapshot | None = None,
) -> Path:
    """Materialize the committed snapshot into *dest* for diffing; return *dest*.

    Probes first unless *snapshot* is given. Any failure is raised as the
    typed state-download error: without the previous state a diff cannot
    detect deletions and backfill cannot detect tables new to the filter, so
    the task must fail and retry rather than proceed without it.

    Raises:
        StateDownloadError: If the probe or a download fails.
    """
    try:
        if snapshot is None:
            snapshot = await store.probe()
        logger.info("Materializing previous state from %s", store.s3_prefix)
        return await store.materialize(snapshot, dest)
    # conformance: ignore[E004] re-raises as typed StateDownloadError; exception propagates to caller
    except Exception as e:
        from application_sdk.common.incremental.incremental_errors import (  # noqa: PLC0415
            StateDownloadError,
        )

        raise StateDownloadError(cause=e) from e


@deprecated(
    "prepare_previous_state is deprecated; use materialize_previous_state (or "
    "CurrentStateStore.materialize) into RunStateDirs.previous_state — will be "
    "removed in v4.0.0."
)
async def prepare_previous_state(
    connection_qualified_name: str,
    current_state_available: bool,
    current_state_dir: Path,
    application_name: str = "",
) -> Path | None:
    """Materialize the previous state beside *current_state_dir* for comparison.

    .. deprecated:: 3.x
        Use :func:`materialize_previous_state` into
        ``RunStateDirs.previous_state``. The ``{current_state_dir}.previous``
        sibling this writes into is shared by every run that passes the same
        *current_state_dir*. Will be removed in v4.0.0.

    Args:
        connection_qualified_name: The connection qualified name.
        current_state_available: Whether previous state exists in S3
        current_state_dir: Path to current-state directory
        application_name: Optional application name override.

    Returns:
        Path to the previous state directory, or None if no previous state

    Raises:
        StateDownloadError: If the probe or a download fails.
    """
    if not current_state_available:
        return None
    store = CurrentStateStore.for_connection(
        connection_qualified_name, application_name
    )
    return await materialize_previous_state(
        store,
        current_state_dir.parent.joinpath(f"{current_state_dir.name}.previous"),
    )


def copy_non_column_entities(
    transformed_dir: Path,
    current_state_dir: Path,
    copy_workers: int = 4,
) -> dict[str, int]:
    """Copy non-column entity files from transformed to current-state.

    Copies table, schema, and database entity JSON files from the current
    run's transformed output to the current-state directory. These entities
    always use the current transformed data (not ancestral).

    Args:
        transformed_dir: Path to transformed output directory
        current_state_dir: Path to current-state directory
        copy_workers: Number of parallel workers for copy operations

    Returns:
        Dictionary mapping entity type to number of files copied

    Example:
        >>> counts = copy_non_column_entities(transformed_dir, state_dir)
        >>> print(f"Copied {counts['table']} table files")
    """
    copy_counts: dict[str, int] = {}

    for entity_type in [EntityType.TABLE, EntityType.SCHEMA, EntityType.DATABASE]:
        entity_dir = transformed_dir.joinpath(entity_type.value)
        if entity_dir.exists():
            dest_dir = current_state_dir.joinpath(entity_type.value)
            count = copy_directory_parallel(
                entity_dir, dest_dir, max_workers=copy_workers
            )
            copy_counts[entity_type.value] = count
            logger.info(
                "Copied %d %s entity files to current state",
                count,
                entity_type.value,
            )

    return copy_counts


def _copy_columns_from_transformed(
    transformed_dir: Path,
    current_state_dir: Path,
    copy_workers: int = 4,
) -> int:
    """Copy column files from transformed to current-state (lightweight).

    Unlike the old ancestral merge, this simply copies columns from the
    current extraction. NO CHANGE table columns are not carried forward.
    Publish-cache is the source of truth for complete column state.

    Args:
        transformed_dir: Path to transformed output directory
        current_state_dir: Path to current-state directory
        copy_workers: Number of parallel workers for copy operations

    Returns:
        Number of column files copied
    """
    column_dir = transformed_dir.joinpath(EntityType.COLUMN.value)
    if not column_dir.exists():
        return 0

    dest_dir = current_state_dir.joinpath(EntityType.COLUMN.value)
    count = copy_directory_parallel(column_dir, dest_dir, max_workers=copy_workers)
    logger.info("Copied %d column files to current state", count)
    return count


@deprecated(
    "get_persistent_s3_prefix is deprecated here; use "
    "application_sdk.common.incremental.helpers.get_persistent_s3_prefix, where it "
    "lives — it was only ever re-exported here by accident; will be removed in v4.0.0."
)
def get_persistent_s3_prefix(
    connection_qualified_name: str, application_name: str = ""
) -> str:
    """Deprecated alias of :func:`application_sdk.common.incremental.helpers.get_persistent_s3_prefix`.

    .. deprecated:: 3.x
        Import it from :mod:`application_sdk.common.incremental.helpers`. Will
        be removed in v4.0.0.
    """
    return _get_persistent_s3_prefix(connection_qualified_name, application_name)


@deprecated(
    "upload_current_state is deprecated; use CurrentStateStore.commit(), which "
    "writes a manifest and prunes stale keys — will be removed in v4.0.0."
)
async def upload_current_state(
    current_state_dir: Path,
    connection_qualified_name: str,
    application_name: str = "",
) -> str:
    """Commit a directory as the connection's current-state snapshot.

    .. deprecated:: 3.x
        Use :meth:`CurrentStateStore.commit`, which this now delegates to under
        a fresh run ID per call, so the upload becomes the committed snapshot
        (manifest written, stale keys pruned) rather than files the manifest
        ignores. Like ``commit``, it renames the files in *current_state_dir*
        to carry the run stamp. Will be removed in v4.0.0.

    Args:
        current_state_dir: Path to local current-state directory
        connection_qualified_name: The connection qualified name.
        application_name: Optional application name override.

    Returns:
        S3 prefix where current-state was uploaded

    Example:
        >>> s3_prefix = await upload_current_state(
        ...     state_dir,
        ...     connection_qualified_name="default/oracle/1764230875",
        ... )
        >>> print(f"Uploaded to: {s3_prefix}")
    """
    store = CurrentStateStore.for_connection(
        connection_qualified_name, application_name
    )
    # No run ID reaches this legacy signature. A fresh one per call is safe: a
    # retried call leaves its earlier attempt's keys stamped by a third run,
    # which the retry's own commit prunes.
    await store.commit(current_state_dir, f"legacy-upload-{uuid.uuid4().hex}")
    return store.s3_prefix


@deprecated(
    "cleanup_previous_state is deprecated; previous state now lives in the "
    "run-scoped RunStateDirs.previous_state, which goes with the run's output "
    "— will be removed in v4.0.0."
)
def cleanup_previous_state(previous_state_dir: Path | None) -> None:
    """Clean up temporary previous state directory.

    .. deprecated:: 3.x
        Nothing to clean up in the run-scoped layout. Will be removed in
        v4.0.0.

    Removes the temporary directory used for storing previous state
    during the merge operation.

    Args:
        previous_state_dir: Path to temporary previous state directory, or None

    Example:
        >>> cleanup_previous_state(prev_dir)
    """
    if previous_state_dir and previous_state_dir.exists():
        try:
            shutil.rmtree(previous_state_dir)
            logger.info(
                "Cleaned up temporary previous state directory: %s",
                previous_state_dir,
            )
        except Exception:
            # Non-critical cleanup failure - log warning but don't raise
            logger.warning(
                "Failed to clean up temporary previous state directory", exc_info=True
            )


@deprecated(
    "prepare_current_state_directory is deprecated; create_current_state_snapshot "
    "resets its own build directory — will be removed in v4.0.0."
)
def prepare_current_state_directory(current_state_dir: Path) -> None:
    """Clear and recreate current-state directory.

    .. deprecated:: 3.x
        :func:`create_current_state_snapshot` resets its build directory
        itself. Will be removed in v4.0.0.

    Args:
        current_state_dir: Path to current-state directory
    """
    _reset_directory(current_state_dir)


def _reset_directory(directory: Path) -> None:
    """Remove *directory* if present and recreate it empty. Blocking."""
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)


async def create_current_state_snapshot(
    connection_qualified_name: str,
    transformed_dir: Path,
    previous_state_dir: Path | None,
    current_state_dir: Path,
    s3_prefix: str,
    run_id: str,
    application_name: str = "",
    copy_workers: int = 4,
    upload_concurrency: int = 4,
    get_backfill_tables_fn: Callable[[Path, Path | None], set[str] | None]
    | None = None,
    *,
    incremental_diff_dir: Path | None = None,
    state_store: CurrentStateStore | None = None,
) -> CurrentStateResult:
    """Create lightweight current-state snapshot with diff and deletion detection.

    Orchestrates the entire current-state creation process:
    1. Get table scope from transformed data
    2. Clear and prepare current-state directory
    3. Copy non-column entities (tables, schemas, databases)
    4. Copy columns from transformed (lightweight — no ancestral merge)
    5. Create incremental diff with deletion detection (if previous state
       exists) and upload it
    6. Commit current-state via :meth:`CurrentStateStore.commit` — upload,
       manifest last, prune — so the snapshot is replaced whole or not at all

    Every offloaded step is drained on cancellation: an abandoned attempt's
    copy thread cannot keep writing into a directory its retry is reusing.

    Current-state is lightweight — it contains only what was extracted in the
    current run. Publish-cache is the source of truth for complete state.

    Args:
        connection_qualified_name: The connection qualified name.
        transformed_dir: Path to current run's transformed output
        previous_state_dir: Path to previous state (or None for first run)
        current_state_dir: Path where current state will be created
        s3_prefix: S3 prefix for persistent artifacts
        run_id: Workflow run ID for diff naming
        application_name: Optional application name override.
        copy_workers: Number of parallel workers for file operations
        upload_concurrency: Max concurrent object-store requests for the
            current-state and incremental-diff uploads (step 5-6). Fixed at
            this value regardless of scale, so large column counts otherwise
            upload at the same concurrency as a small connection.
        get_backfill_tables_fn: Optional function to detect backfill tables
        incremental_diff_dir: Where to build the diff. Defaults to the legacy
            per-connection ``runs/{run_id}/incremental-diff`` directory; the
            template passes the run-scoped ``RunStateDirs.diff``.
        state_store: The store to commit through. Defaults to one for
            ``{s3_prefix}/current-state``.

    Returns:
        CurrentStateResult with paths and statistics

    Raises:
        FileNotFoundError: If no tables found in transformed output

    Example:
        >>> result = await create_current_state_snapshot(
        ...     connection_qualified_name="default/oracle/1764230875",
        ...     transformed_dir=Path("./transformed"),
        ...     previous_state_dir=Path("./previous-state"),
        ...     current_state_dir=Path("./current-state"),
        ...     s3_prefix="persistent-artifacts/apps/oracle/conn/123",
        ...     run_id="abc123",
        ... )
        >>> print(f"Created {result.total_files} files")
    """
    store = state_store or CurrentStateStore(f"{s3_prefix}/{CURRENT_STATE_SUBPATH}")
    table_scope = None
    diff_result = None
    diff_dir: Path | None = None
    incremental_diff_s3_prefix = None

    with DuckDBConnectionManager() as conn_manager:
        conn = conn_manager.connection

        try:
            # Step 1: Get table scope (qualified names and incremental states)
            #
            # Every sync step in this block is offloaded for the same reason:
            # each one is a DuckDB scan over the run's transformed output or a
            # blocking parallel directory copy, none of them yield, and their
            # cost scales with the source. Inline they hold the event loop —
            # and this activity's auto-heartbeat — for the whole snapshot, so a
            # healthy large-connection run gets killed on heartbeat_timeout
            # (ADR-0010). The DuckDB connection is `threads=1` and file-backed
            # and each hop is awaited before the next starts, so it is only
            # ever touched by one thread at a time.
            table_scope = await _run_drained(
                run_in_thread(get_current_table_scope, transformed_dir, conn=conn)
            )
            if not table_scope or get_scope_length(table_scope) == 0:
                raise FileNotFoundError(
                    f"No tables found in transformed output: {transformed_dir}. "
                    "Cannot create current state without table metadata."
                )

            logger.info(
                "Creating current-state snapshot: %d tables",
                get_scope_length(table_scope),
            )

            # Step 2: Clear and prepare current-state directory. A same-run
            # retry reuses it, so whatever an earlier attempt left is dropped.
            await _run_drained(run_in_thread(_reset_directory, current_state_dir))

            # Step 3: Copy non-column entities (tables, schemas, databases)
            await _run_drained(
                run_in_thread(
                    copy_non_column_entities,
                    transformed_dir=transformed_dir,
                    current_state_dir=current_state_dir,
                    copy_workers=copy_workers,
                )
            )

            # Step 4: Copy columns from transformed (lightweight — no ancestral merge)
            column_count = await _run_drained(
                run_in_thread(
                    _copy_columns_from_transformed,
                    transformed_dir=transformed_dir,
                    current_state_dir=current_state_dir,
                    copy_workers=copy_workers,
                )
            )

            # Track which tables have extracted columns
            tables_with_columns = (
                await _run_drained(
                    run_in_thread(
                        get_table_qns_from_columns,
                        current_state_dir.joinpath(EntityType.COLUMN.value),
                        conn=conn,
                    )
                )
                or set()
            )
            table_scope.tables_with_extracted_columns = tables_with_columns

            total_files = await _run_drained(
                run_in_thread(count_json_files_recursive, current_state_dir)
            )

            logger.info(
                "Current-state snapshot complete (lightweight): tables=%d columns_copied=%d "
                "tables_with_columns=%d total_files=%d",
                get_scope_length(table_scope),
                column_count,
                len(tables_with_columns),
                total_files,
            )

            # Step 5: Create incremental-diff (only changed assets from this run)
            if previous_state_dir and previous_state_dir.exists():
                incremental_diff_subpath = INCREMENTAL_DIFF_SUBPATH_TEMPLATE.format(
                    run_id=run_id
                )
                diff_dir = incremental_diff_dir or get_persistent_artifacts_path(
                    connection_qualified_name,
                    incremental_diff_subpath,
                    application_name,
                )
                incremental_diff_s3_prefix = f"{s3_prefix}/{incremental_diff_subpath}"

                # Clear the diff directory: a same-run retry reuses it.
                if diff_dir.exists():
                    await _run_drained(run_in_thread(shutil.rmtree, diff_dir))

                diff_result = await _run_drained(
                    run_in_thread(
                        create_incremental_diff,
                        transformed_dir=transformed_dir,
                        incremental_diff_dir=diff_dir,
                        table_scope=table_scope,
                        previous_state_dir=previous_state_dir,
                        conn=conn,
                        copy_workers=copy_workers,
                        get_backfill_tables_fn=get_backfill_tables_fn,
                    )
                )

                # Upload incremental-diff to S3
                await upload_prefix(
                    local_dir=str(diff_dir),
                    prefix=incremental_diff_s3_prefix,
                    max_concurrency=upload_concurrency,
                )
                logger.info(
                    "Incremental-diff uploaded to S3: %s",
                    incremental_diff_s3_prefix,
                )
            else:
                logger.info(
                    "Skipping incremental-diff creation "
                    "(first run - no previous state to diff against)"
                )

        finally:
            # Close the TableScope's disk-backed stores
            if table_scope:
                close_scope(table_scope)

    # Step 6: Commit current-state — upload, manifest last, prune. Only after
    # the diff is uploaded: the committed snapshot is what the next run diffs
    # against, so it must not move before this run's diff is durable.
    snapshot = await store.commit(
        current_state_dir, run_id, max_concurrency=upload_concurrency
    )

    return CurrentStateResult(
        current_state_dir=current_state_dir,
        current_state_s3_prefix=store.s3_prefix,
        total_files=total_files,
        incremental_diff_dir=diff_dir,
        incremental_diff_s3_prefix=incremental_diff_s3_prefix,
        incremental_diff_files=diff_result.total_files if diff_result else 0,
        snapshot=snapshot,
    )
