"""Release a run's per-worker state on this worker once the run has closed.

Two kinds of per-run state sit in a worker process, on whichever workers ran
the run's tasks:

* the credential directories ``credentials.utils`` wrote (a keytab, a private
  key), and
* the run's ``_app_state`` slot: the ``FileReference`` objects its tasks
  tracked and whatever the app cached there (an ``AsyncAtlanClient``, a
  source client).

A run's own ``cleanup_files`` reaches only the worker it runs on, and the
workflow never touches a worker's slot. So each worker sweeps its own: it asks
Temporal whether each run that left state behind has closed, and for every
closed run removes the credential directories and releases the slot — its
tracked local files deleted, each value closed. Whatever is left is released
when the worker shuts down.

The Temporal client calls live here, in the execution adapter; the registries
they read are ``app.base``'s.
"""

from __future__ import annotations

import asyncio

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from application_sdk._logging import get_logger

logger = get_logger(__name__)

#: How often a worker asks Temporal about runs with state left behind.
#: A sweep with nothing tracked makes no calls.
SWEEP_INTERVAL_SECONDS = 60.0


async def _run_is_closed(client: Client, workflow_id: str, run_id: str) -> bool:
    """Whether Temporal reports the run as no longer running.

    A run Temporal no longer knows (past retention) counts as closed. Any
    other failure to ask counts as open, so the directory is kept and the
    next sweep asks again.
    """
    try:
        description = await client.get_workflow_handle(
            workflow_id, run_id=run_id
        ).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return True
        logger.warning(
            "Could not read run status for the run-state sweep; "
            "retrying on the next sweep",
            exc_info=True,
        )
        return False
    return description.status != WorkflowExecutionStatus.RUNNING


async def sweep_closed_runs(client: Client) -> None:
    """Release this worker's state of every closed run.

    For each run with credential directories or an app-state slot here that
    Temporal reports closed, removes the directories and releases the slot.
    """
    from application_sdk.app.base import (  # noqa: PLC0415 — circular: app.base imports the execution package
        _release_run_state,
        _remove_run_credential_dirs,
        _runs_with_app_state,
        _runs_with_credential_dirs,
    )

    runs = dict.fromkeys([*_runs_with_credential_dirs(), *_runs_with_app_state()])
    for workflow_id, run_id in runs:
        if await _run_is_closed(client, workflow_id, run_id):
            await _remove_run_credential_dirs((workflow_id, run_id))
            await _release_run_state((workflow_id, run_id))


async def remove_all_credential_dirs() -> None:
    """Remove every credential directory this worker still tracks."""
    from application_sdk.app.base import (  # noqa: PLC0415 — circular: app.base imports the execution package
        _remove_run_credential_dirs,
        _runs_with_credential_dirs,
    )

    for run in _runs_with_credential_dirs():
        await _remove_run_credential_dirs(run)


async def release_all_runs() -> None:
    """Release every run's state on this worker (shutdown).

    Removes every credential directory, then releases every app-state slot,
    closing what each holds. Runs after the Temporal worker has stopped, so
    no task on this worker can still be using either.
    """
    from application_sdk.app.base import (  # noqa: PLC0415 — circular: app.base imports the execution package
        _drain_state_releases,
        _release_run_state,
        _runs_with_app_state,
    )

    await remove_all_credential_dirs()
    # A release the stopped sweeper had under way has already taken its slot
    # out of _app_state, so it is finished here, not enumerated below.
    await _drain_state_releases()
    for run in _runs_with_app_state():
        await _release_run_state(run)


async def run_sweeper(
    client: Client, interval_seconds: float = SWEEP_INTERVAL_SECONDS
) -> None:
    """Sweep every *interval_seconds* until cancelled."""
    while True:
        # conformance: ignore[P060] fixed-cadence sweeper interval, not a retry ladder
        await asyncio.sleep(interval_seconds)
        try:
            await sweep_closed_runs(client)
        # conformance: ignore[E004] a background sweep must outlive one failed pass; logged, retried next interval
        except Exception:
            logger.warning(
                "Run-state sweep failed; retrying next interval",
                exc_info=True,
            )
