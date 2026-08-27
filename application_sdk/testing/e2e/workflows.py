"""Workflow trigger and status helpers for K8s e2e tests."""

from datetime import timedelta
from typing import Any

import httpx

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.cluster import kube_http_call, port_forward
from application_sdk.testing.harness.outcome import Expired, assert_settled
from application_sdk.testing.harness.waiting import poll_until

logger = get_logger(__name__)

_TERMINAL_STATES = {"completed", "failed", "cancelled", "terminated"}

#: Consecutive unreadable polls to absorb before the wait declares it has no
#: verdict. Five at the 5-second default is ~25s of an unreachable handler,
#: which is longer than a pod restart and shorter than any real wait — the same
#: streak length the AE poll uses, for the same reason.
_MAX_TRANSIENT_FAILURES = 5


def _absorb_a_handler_blip(error: BaseException) -> timedelta | None:
    """Is a failed status read worth another poll, or is it the answer?

    The gap this closes: the loop this replaced called ``raise_for_status()``
    with no policy around it, so one 502 from a handler pod mid-restart aborted
    a 300-second wait — and aborted it as an ``httpx.HTTPStatusError``, which
    reads as a test failure rather than as an unreadable dependency.

    The narrowing is by *status class*, not by "any HTTP error":

    * a transport error, or a 5xx / 429, is the handler being unreachable or
      overloaded, and the next poll is entitled to a different answer;
    * a 4xx is the handler answering. A 404 on a workflow id is a wrong id, and
      no amount of waiting turns it into the right one — absorbing it would
      spend the whole budget on a typo and then report a timeout.

    Returns:
        ``timedelta(0)`` — "absorb this, the loop's own interval is the gap" —
        for a blip, or ``None`` for anything terminal. The handler serves no
        ``Retry-After``, so there is no origin backoff to honour.
    """
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        retryable = status >= 500 or status == httpx.codes.TOO_MANY_REQUESTS
        return timedelta(0) if retryable else None
    if isinstance(error, httpx.TransportError):
        return timedelta(0)
    return None


async def run_workflow(
    namespace: str,
    service: str,
    port: int,
    workflow_name: str,
    payload: dict[str, Any],
) -> str:
    """POST to the handler's workflow endpoint and return the workflow ID.

    Args:
        namespace: K8s namespace where the handler service lives.
        service: Handler service name.
        port: Handler service port.
        workflow_name: Name of the workflow to trigger.
        payload: JSON body for the workflow request.

    Returns:
        The workflow ID string from the response.
    """
    response = await kube_http_call(
        namespace=namespace,
        service=service,
        port=port,
        method="POST",
        path="/api/v1/workflows",
        body={"workflow_name": workflow_name, **payload},
    )
    response.raise_for_status()
    data = response.json()
    workflow_id: str = data["workflow_id"]
    logger.info("Started workflow %s (id=%s)", workflow_name, workflow_id)
    return workflow_id


async def wait_for_workflow(
    namespace: str,
    service: str,
    port: int,
    workflow_id: str,
    timeout: float = 300.0,
    poll_interval: float = 5.0,
) -> dict[str, Any]:
    """Poll GET /api/v1/workflows/{id} until the workflow reaches a terminal state.

    One ``kubectl port-forward`` tunnel serves the whole poll. It used to open and
    tear one down per probe — up to 60 ``kubectl`` processes, handshakes and
    readiness waits inside a single 300-second wait, to read one status string.
    The per-call teardown's stated reason was idle TCP timeouts on a long-lived
    forward, which a 5-second cadence never reaches; the case it was really
    buying, a tunnel that has already died, is handled by
    :meth:`~application_sdk.testing.harness.cluster.PortForward.request`
    rebuilding once on a transport error.

    The loop is :func:`~application_sdk.testing.harness.waiting.poll_until`
    (FND-240), and that is what gives this wait a **transient-error policy** — it
    had none. ``raise_for_status()`` sat bare inside the old loop, so a single
    502 from a handler pod mid-restart ended a five-minute wait on its first
    poll. :func:`_absorb_a_handler_blip` decides which failed reads are worth
    another poll and which are the answer.

    ``TimeoutError`` is kept for the expired case rather than becoming
    :class:`~application_sdk.testing.harness._errors.WaitExpiredError`: this is a
    public export with out-of-repo consumers, and their ``except TimeoutError``
    is not something a refactor gets to invalidate. The unreadable case has no
    such history — it used to surface as a raw ``httpx`` error — so it raises the
    typed leaf.

    Args:
        namespace: K8s namespace where the handler service lives.
        service: Handler service name.
        port: Handler service port.
        workflow_id: Workflow ID returned by :func:`run_workflow`.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.

    Returns:
        The final workflow status dict from the last response.

    Raises:
        TimeoutError: If the workflow does not complete within ``timeout``.
        WaitIndeterminateError: If the handler could not be read for
            :data:`_MAX_TRANSIENT_FAILURES` consecutive polls. Distinct from the
            timeout on purpose: an unreachable handler is not evidence about the
            workflow, and grading it as one would report a pod restart as a
            workflow that never finished.
    """
    # One session for the whole poll, opened outside the probe. Opening it
    # *inside* would restore the per-probe ``kubectl`` process FND-241 removed —
    # the probe is the loop body, and a context manager in a loop body is a
    # tunnel per iteration.
    async with port_forward(namespace, service, port) as session:

        async def probe() -> dict[str, Any]:
            response = await session.request("GET", f"/api/v1/workflows/{workflow_id}")
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            logger.debug("Workflow %s status: %s", workflow_id, _status_of(data))
            return data

        outcome = await poll_until(
            probe,
            settled=lambda data: _status_of(data) in _TERMINAL_STATES,
            transient=_absorb_a_handler_blip,
            budget=Budget(
                timeout=timedelta(seconds=timeout),
                poll_interval=timedelta(seconds=poll_interval),
                max_transient_failures=_MAX_TRANSIENT_FAILURES,
            ),
            label=f"workflow {workflow_id}",
        )
    if isinstance(outcome, Expired):
        raise TimeoutError(
            f"Workflow {workflow_id} did not reach a terminal state within "
            f"{timeout}s ({outcome.attempts} attempts, "
            f"{outcome.elapsed.total_seconds():.0f}s elapsed; last status="
            f"{_status_of(outcome.last) or 'unknown'})"
        )
    # Indeterminate keeps its typed leaf; Settled unwraps. Neither NeverStarted
    # nor Stalled is reachable — no ``started`` predicate and no ``fingerprint``
    # is passed, so neither guard is armed — and ``assert_settled`` covers both
    # anyway rather than this function growing a branch per unreachable verdict.
    return assert_settled(outcome)


def _status_of(data: dict[str, Any] | None) -> str:
    """The handler's status string, lowercased; ``""`` when there is none.

    Total on the input rather than assuming a dict with a string ``status``: the
    settled predicate, the debug line and the timeout message all read it, and a
    handler that answered ``{"status": null}`` must not crash the wait in the
    predicate that was deciding whether to keep waiting.
    """
    if not isinstance(data, dict):
        return ""
    status = data.get("status", "")
    return status.lower() if isinstance(status, str) else ""
