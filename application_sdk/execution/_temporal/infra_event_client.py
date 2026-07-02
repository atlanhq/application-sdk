"""Client boundary for the (net-new) infra-event service.

The service answers one question: *what happened to this pod/node around the
time an activity stopped heartbeating?* It ingests spot-interruption notices,
Kubernetes pod ``lastState.terminated`` reasons (``OOMKilled`` / exit 137), and
node-lifecycle events (Karpenter, node-termination-handler) into a durable,
queryable store. Durable matters: pods are garbage-collected and Kubernetes
Events expire (~1h), so a *live* query at diagnosis time often finds nothing.

This module is the SDK-side *consumer* only. The service itself is a separate
build; until it exists the client is disabled by default and every lookup
returns :meth:`InfraVerdict.none`, so diagnosis fails safe. See
``docs/infra-failure-diagnosis.md``.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any, Protocol


class InfraCause(str, Enum):
    """Vocabulary the infra-event service returns for a stopped activity."""

    SPOT_RECLAIM = "SPOT_RECLAIM"
    OOM_KILLED = "OOM_KILLED"
    NODE_LOST = "NODE_LOST"
    NONE = "NONE"
    """No infra event found for this pod/node/window — treat as 'cause unknown',
    not 'confirmed not-infra'. Could be a genuinely slow activity, or an event
    the service missed."""


@dataclasses.dataclass(frozen=True)
class ActivityLocation:
    """Where/when an activity was last known to be running.

    The correlation key for an infra-event lookup, assembled by the workflow
    from the failed activity's worker identity and last heartbeat details.
    """

    node: str | None = None
    pod: str | None = None
    started_at: str | None = None
    """ISO-8601, from the seeded identity heartbeat. Lower bound of the window."""
    last_heartbeat_at: str | None = None
    """ISO-8601 of the last heartbeat. Approximates time-of-death (upper bound)."""

    def is_correlatable(self) -> bool:
        """True iff there is at least a node or pod to query by."""
        return bool(self.node or self.pod)


@dataclasses.dataclass(frozen=True)
class InfraVerdict:
    """Answer from the infra-event service."""

    cause: InfraCause
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def none(cls, reason: str) -> InfraVerdict:
        """A fail-safe 'no infra cause' verdict carrying why we couldn't tell."""
        return cls(cause=InfraCause.NONE, evidence={"reason": reason})


class InfraEventClient(Protocol):
    """Queries the infra-event service.

    Implementations MUST fail safe: on any error they return
    :meth:`InfraVerdict.none` rather than raising, so diagnosis never blocks
    or fails a workflow.
    """

    async def lookup(self, location: ActivityLocation) -> InfraVerdict: ...


class FakeInfraEventClient:
    """In-memory client for tests and local runs.

    Pre-seed verdicts keyed by pod (falling back to node); unknown keys
    return ``NONE``.
    """

    def __init__(self, verdicts: dict[str, InfraVerdict] | None = None) -> None:
        self._verdicts = verdicts or {}

    async def lookup(self, location: ActivityLocation) -> InfraVerdict:
        key = location.pod or location.node or ""
        return self._verdicts.get(key, InfraVerdict.none("no seeded verdict"))


class HttpInfraEventClient:
    """Real client for the net-new infra-event service.

    Contract::

        POST {base_url}/infra-events/lookup
          { "node": str, "pod": str, "since": iso8601, "until": iso8601 }
        -> { "cause": "SPOT_RECLAIM|OOM_KILLED|NODE_LOST|NONE",
             "evidence": { ... } }

    Fails safe: timeouts, non-200, and malformed bodies all return ``NONE``.
    """

    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def lookup(self, location: ActivityLocation) -> InfraVerdict:
        # TODO(service): implement once the infra-event service ships.
        # Use an async client (httpx.AsyncClient) — this runs inside an
        # activity on Temporal's event loop; a blocking client would stall it.
        #
        #   payload = {"node": location.node, "pod": location.pod,
        #              "since": location.started_at, "until": location.last_heartbeat_at}
        #   try:
        #       async with httpx.AsyncClient(timeout=self._timeout_seconds) as c:
        #           resp = await c.post(f"{self._base_url}/infra-events/lookup", json=payload)
        #       resp.raise_for_status()
        #       body = resp.json()
        #       return InfraVerdict(cause=InfraCause(body["cause"]),
        #                           evidence=body.get("evidence", {}))
        #   except Exception:
        #       return InfraVerdict.none("lookup failed")   # never raise
        return InfraVerdict.none("HttpInfraEventClient not implemented")


class _DisabledInfraEventClient:
    """Returned when the feature is off or unconfigured. Always ``NONE``."""

    async def lookup(self, location: ActivityLocation) -> InfraVerdict:
        return InfraVerdict.none("infra diagnosis disabled")


def get_infra_event_client() -> InfraEventClient:
    """Return the configured client, or a fail-safe stub when disabled.

    Read at activity time (inside :func:`diagnose_infra_failure`) so the flag
    can be toggled per-deployment without a workflow code change.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — read at call time so tests/deploys can toggle
        ENABLE_INFRA_FAILURE_DIAGNOSIS,
        INFRA_EVENT_SERVICE_URL,
    )

    if not ENABLE_INFRA_FAILURE_DIAGNOSIS or not INFRA_EVENT_SERVICE_URL:
        return _DisabledInfraEventClient()
    return HttpInfraEventClient(INFRA_EVENT_SERVICE_URL)
