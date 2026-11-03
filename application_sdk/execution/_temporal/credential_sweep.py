"""Remove credential directories on this worker once their run has closed.

A run's ``cleanup_files`` removes the credential directories it finds on the
worker it runs on. Temporal can schedule it on a different worker from the
task that wrote them, so each worker also sweeps its own: it asks Temporal
whether each run that left directories behind has closed, and removes those
of the closed runs. Whatever is left is removed when the worker shuts down.

The Temporal client calls live here, in the execution adapter; the registry
they read is ``app.base``'s, which ``credentials.utils`` writes through.
"""

from __future__ import annotations

import asyncio

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from application_sdk._logging import get_logger

logger = get_logger(__name__)

#: How often a worker asks Temporal about runs with directories left behind.
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
            "Could not read run status for credential directory sweep; "
            "retrying on the next sweep",
            exc_info=True,
        )
        return False
    return description.status != WorkflowExecutionStatus.RUNNING


async def sweep_closed_runs(client: Client) -> None:
    """Remove the credential directories of every closed run on this worker."""
    from application_sdk.app.base import (  # noqa: PLC0415 — circular: app.base imports the execution package
        _remove_run_credential_dirs,
        _runs_with_credential_dirs,
    )

    for workflow_id, run_id in _runs_with_credential_dirs():
        if await _run_is_closed(client, workflow_id, run_id):
            await _remove_run_credential_dirs((workflow_id, run_id))


async def remove_all_credential_dirs() -> None:
    """Remove every credential directory this worker still tracks (shutdown)."""
    from application_sdk.app.base import (  # noqa: PLC0415 — circular: app.base imports the execution package
        _remove_run_credential_dirs,
        _runs_with_credential_dirs,
    )

    for run in _runs_with_credential_dirs():
        await _remove_run_credential_dirs(run)


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
                "Credential directory sweep failed; retrying next interval",
                exc_info=True,
            )
