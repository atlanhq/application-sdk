"""Deterministic, cross-worker GC for run-scoped local ``FileReference`` scratch.

The local directory layout *is* the registry: every run's scratch lives under
``{LOCAL_FILE_REF_ROOT}/{workflow_id}/{run_id}/``. :func:`sweep_local_file_refs`
walks that tree and, for each run dir, asks Temporal for the run's status via
``describe()`` to decide whether the run is over and its scratch can be removed.

No separate index and no cross-worker coordination: each worker GCs its *own*
filesystem at its next activity by querying the authoritative source (Temporal).
A worker that held orphaned copies cleans them the next time it runs anything.

Decision table (per run dir):

* COMPLETED / FAILED / CANCELED / TERMINATED / TIMED_OUT / CONTINUED_AS_NEW
  → delete
* RUNNING → keep
* ``RPCError(NOT_FOUND)`` (history GC'd / never ran) → delete
* connection / transient error → keep (retry next sweep)
* dir == current run → skip (never described)
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from temporalio.client import Client

logger = get_logger(__name__)

#: Statuses that mean the run is over and its local scratch is safe to delete.
_TERMINAL_STATUSES = frozenset(
    {
        WorkflowExecutionStatus.COMPLETED,
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.CANCELED,
        WorkflowExecutionStatus.TERMINATED,
        WorkflowExecutionStatus.TIMED_OUT,
        WorkflowExecutionStatus.CONTINUED_AS_NEW,
    }
)


def run_scoped_dir(
    workflow_id: str,
    run_id: str,
    *,
    root: str | None = None,
    create: bool = False,
) -> Path:
    """Return ``{root}/{sanitize(workflow_id)}/{run_id}/`` — the GC path scheme.

    This is the single definition of the run-scoped local layout. ``App.local_run_dir()``
    (where apps write scratch), the activity auto-materialize directory, and
    :func:`sweep_local_file_refs` (which walks the tree) all derive from it, so
    the layout can never drift between writer and collector.

    Args:
        workflow_id: Current workflow id. Path separators are collapsed to a
            single safe segment so the run dir never nests unexpectedly.
        run_id: Current run id (used verbatim as the leaf segment).
        root: Local root. Defaults to ``LOCAL_FILE_REF_ROOT`` (read at call time
            so the env-driven default stays patchable in tests).
        create: When True, create the directory (``parents=True, exist_ok=True``).

    Returns:
        The run-scoped :class:`~pathlib.Path`.
    """
    if root is None:
        from application_sdk.constants import (  # noqa: PLC0415 — read at call time so the env-driven default stays patchable
            LOCAL_FILE_REF_ROOT,
        )

        root = LOCAL_FILE_REF_ROOT
    safe_workflow_id = re.sub(r"[/\\]+", "_", workflow_id)
    path = Path(root) / safe_workflow_id / run_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class SweepResult:
    """Outcome of a single :func:`sweep_local_file_refs` pass.

    Attributes:
        deleted: Run-dir paths removed (terminal status or NOT_FOUND).
        kept: Run-dir paths left in place (RUNNING, a transient describe error,
            or a delete that failed and will be retried next sweep).
        describes: Number of ``describe()`` RPCs issued — bounded by ``max_describes``.
    """

    deleted: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    describes: int = 0


async def _describe_status(
    client: Client, workflow_id: str, run_id: str
) -> WorkflowExecutionStatus | None:
    """Return the run's ``WorkflowExecutionStatus``, or ``None`` for NOT_FOUND.

    ``None`` means Temporal has no record of the run (history GC'd or it never
    existed) — the caller treats that as "safe to delete". Any non-NOT_FOUND
    ``RPCError`` (and any other exception) propagates so the caller can keep the
    dir and retry on the next sweep.
    """
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    try:
        desc = await handle.describe()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    return desc.status


def _rmtree(run_dir: Path) -> bool:
    """Best-effort recursive delete. Returns True on success.

    A failed delete is swallowed (logged) so one undeletable dir never aborts
    the sweep; the dir is kept and retried on the next pass.
    """
    try:
        shutil.rmtree(run_dir)
        return True
    except OSError:
        logger.warning(
            "local_gc: failed to delete run dir, will retry next sweep",
            run_dir=str(run_dir),
            exc_info=True,
        )
        return False


async def sweep_local_file_refs(
    client: Client,
    *,
    current_run_id: str,
    max_describes: int,
    root: str | None = None,
) -> SweepResult:
    """Walk the run-scoped local root and delete scratch for finished runs.

    Args:
        client: Connected Temporal client used to ``describe()`` each run.
        current_run_id: Run id of the sweeping activity. Its own dir is always
            skipped (and never described) so a live run never deletes itself.
        max_describes: Upper bound on ``describe()`` RPCs this sweep issues.
            Run dirs beyond the cap are left for the next sweep.
        root: Local root to walk. Defaults to ``LOCAL_FILE_REF_ROOT`` (read at
            call time so tests can point it at a temp dir).

    Returns:
        A :class:`SweepResult` summarising deletes, keeps, and describes issued.
    """
    if root is None:
        from application_sdk.constants import (  # noqa: PLC0415 — read at call time so the env-driven default is patchable in tests
            LOCAL_FILE_REF_ROOT,
        )

        root = LOCAL_FILE_REF_ROOT

    result = SweepResult()
    base = Path(root)
    if not base.is_dir():
        return result

    for wf_dir in sorted(base.iterdir()):
        if not wf_dir.is_dir():
            continue
        for run_dir in sorted(wf_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            # The live run's own scratch is off-limits — never describe or delete it.
            if run_dir.name == current_run_id:
                continue
            # Budget spent: leave the rest for a later sweep (self-healing).
            if result.describes >= max_describes:
                return result

            result.describes += 1
            try:
                status = await _describe_status(client, wf_dir.name, run_dir.name)
            except Exception:
                # Transient / connection error — keep the dir, retry next sweep.
                logger.warning(
                    "local_gc: describe failed, keeping run dir for next sweep",
                    run_dir=str(run_dir),
                    exc_info=True,
                )
                result.kept.append(str(run_dir))
                continue

            if status is None or status in _TERMINAL_STATUSES:
                if _rmtree(run_dir):
                    result.deleted.append(str(run_dir))
                else:
                    result.kept.append(str(run_dir))
            else:
                result.kept.append(str(run_dir))

    return result
