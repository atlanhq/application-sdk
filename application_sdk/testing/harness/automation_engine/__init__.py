"""Automation Engine reads and write retries, lifted out of ``testing/e2e/client.py``.

The other half of the ``client.py`` split (child F on FND-224); see
:mod:`application_sdk.testing.harness.atlas` for the reasoning shared by both.

What is specific to this half is the **submit**, which is the one non-idempotent
write in the harness. Re-issuing a submit the origin already processed spawns a
duplicate run, so the retry logic cannot simply count attempts — it has to know
whether a failed request can have taken effect. ``testing/e2e/_errors.py``'s
``RequestDelivery`` already encodes that (a connect-phase failure never left the
client; a follow-up read that finds no trace proves it was not applied), and it
lifts across with the submit rather than being re-derived.

The retry budget here is also the widest in the harness and for a
counter-intuitive reason: an AE submit is the *only* tenant-facing probe of the
installed app pod on the connector CI path, because the runner has no kubectl
route into the tenant vcluster. A pod that is minutes from serving arrives as a
generic 5xx on submit, so the submit's own retry has to be sized for a cold
start rather than for transient AE overload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from application_sdk.testing.harness._errors import HarnessNotBuiltError
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import Outcome

__all__ = ["AERunHandle", "NativeStatus", "poll_native_status", "submit_run"]


@dataclass(frozen=True, slots=True, kw_only=True)
class AERunHandle:
    """What a successful submit returns.

    Attributes:
        workflow_slug: AE's slug for the workflow the run belongs to.
        run_id: AE's identifier for this run.
    """

    workflow_slug: str
    run_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeStatus:
    """One reading of a run's ``native-status``.

    Attributes:
        node_states: DAG node name -> state, as AE reports it.
        finished: Whether AE considers the run terminal.
        fingerprint: The node-state summary used as the progress fingerprint by
            :func:`~application_sdk.testing.harness.waiting.poll_until`. Carried
            on the reading rather than recomputed by the caller so the value the
            watchdog compares and the value the report prints cannot drift.
    """

    node_states: Mapping[str, str]
    finished: bool
    fingerprint: str


async def submit_run(
    payload: Mapping[str, object], *, budget: Budget
) -> Outcome[AERunHandle]:
    """Submit a run to the Automation Engine, retrying only where it is safe to.

    Args:
        payload: The AE submit body.
        budget: Retry allowance. Sized for a tenant app pod's cold start, not for
            transient AE overload — see the module docstring.

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` carrying the
        handle, or a verdict describing why no run was started.

    Raises:
        HarnessNotBuiltError: Always — implementation is child F on FND-224.
    """
    raise HarnessNotBuiltError(
        message="submit_run is not implemented yet",
        operation="submit_run",
        reason="child F on FND-224",
        issue="FND-224",
        component="harness_automation_engine",
    )


async def poll_native_status(
    handle: AERunHandle, *, budget: Budget
) -> Outcome[NativeStatus]:
    """Poll a run's native status until it settles, stalls or runs out of budget.

    Re-expressed over :func:`~application_sdk.testing.harness.waiting.poll_until`
    rather than re-implemented: the start-grace latch and the no-change watchdog
    this function needs are exactly what that primitive was extracted from.

    Args:
        handle: The run to poll.
        budget: The poll's allowance.

    Returns:
        The verdict, carrying the last :class:`NativeStatus` read.

    Raises:
        HarnessNotBuiltError: Always — implementation is child F on FND-224.
    """
    raise HarnessNotBuiltError(
        message="poll_native_status is not implemented yet",
        operation="poll_native_status",
        reason="child F on FND-224",
        issue="FND-224",
        component="harness_automation_engine",
    )
