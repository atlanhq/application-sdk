"""Attribution of abrupt worker deaths (spot reclaim / OOM / node loss).

When an activity dies *without* reporting ``WorkerEvicted`` — the signature of a
kill the worker could not survive (spot reclaim, OOMKill, node loss) — Temporal
fails the attempt with a **heartbeat timeout**. Unlike a graceful eviction (see
``eviction_retry.py``), nothing in-process ran to say why. This module turns
that silent timeout into a recorded cause:

  1. the workflow decodes *where* the activity was running (worker identity +
     last heartbeat details — both survive the kill),
  2. runs the :func:`diagnose_infra_failure` activity, which asks the
     infra-event service what happened to that pod/node,
  3. maps the verdict onto the SDK failure taxonomy (:mod:`application_sdk.errors`)
     and records it (Temporal search attribute + structured log), then re-raises
     the original failure unchanged.

**Phase 1 is attribution only.** Behaviour is unchanged: the activity still
fails, the workflow still sees the same error. Recording the cause does *not*
stop spot/OOM from consuming ``max_attempts`` — only rerouting the retry to a
different task queue does, and that (Phase 2) is gated on a second, on-demand
worker pool that does not exist yet. See ``docs/infra-failure-diagnosis.md``.

**Determinism.** Everything that runs in workflow context — the predicates,
:func:`classify_infra_failure`, :func:`workflow.upsert_search_attributes` — is
pure and replay-safe. All I/O (the service call) lives in the
:func:`diagnose_infra_failure` *activity*. The correlation time window comes
from heartbeat details, never a wall clock.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from temporalio import activity, workflow
from temporalio.common import SearchAttributeKey
from temporalio.exceptions import ActivityError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.exceptions import TimeoutType

from application_sdk.execution._temporal.eviction_retry import (
    execute_activity_with_eviction_retry,
)
from application_sdk.execution._temporal.infra_event_client import (
    ActivityLocation,
    InfraVerdict,
    get_infra_event_client,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: Temporal wire name of the diagnosis activity. Must be registered on the
#: worker (see docs → "Wiring").
DIAGNOSE_ACTIVITY_NAME = "sdk:diagnose_infra_failure"

#: Keyword search attribute recording the diagnosed cause on the workflow.
#: MUST be registered on the namespace as a Keyword attribute before use
#: (``tctl admin cluster add-search-attributes`` / operator API).
INFRA_FAILURE_CAUSE_ATTR = "infra_failure_cause"
_INFRA_CAUSE_KEY = SearchAttributeKey.for_keyword(INFRA_FAILURE_CAUSE_ATTR)


# ---------------------------------------------------------------------------
# workflow-side: detection
# ---------------------------------------------------------------------------


def _is_heartbeat_timeout(err: ActivityError) -> bool:
    """True iff this ``ActivityError`` was caused by a *heartbeat* timeout.

    A heartbeat timeout with no preceding ``WorkerEvicted`` is the signature of
    an abrupt kill (spot reclaim, OOMKill, node loss): the worker died before it
    could report anything. Start-to-close / schedule timeouts are deliberately
    excluded — those usually mean genuinely slow work, not a dead worker.

    Contract note (mirrors ``_is_worker_evicted``): relies on
    ``ActivityError.cause`` returning ``__cause__`` and on
    ``temporalio.exceptions.TimeoutError.type``. Pinned by the fixture in
    ``tests/unit/execution/test_infra_diagnosis.py``.
    """
    cause = err.cause
    return (
        isinstance(cause, TemporalTimeoutError) and cause.type == TimeoutType.HEARTBEAT
    )


def _decode_activity_location(err: ActivityError) -> ActivityLocation:
    """Best-effort extraction of ``{node, pod, started_at}`` from a failure.

    Two carriers, tried in order:

    1. **Seeded heartbeat details** — the identity dict recorded as the first
       heartbeat detail by :func:`seed_activity_identity`. Survives into
       ``TimeoutError.last_heartbeat_details``.
    2. **Worker identity** (``ActivityError.identity``) — the clobber-proof
       fallback, parsed from ``"{pod}@{node}"`` when the worker sets it.

    Never raises: returns an empty :class:`ActivityLocation` when nothing is
    decodable (e.g. OOM before the first heartbeat and no worker identity set).
    """
    node = pod = started_at = last_heartbeat_at = None

    cause = err.cause
    if isinstance(cause, TemporalTimeoutError):
        details = tuple(cause.last_heartbeat_details or ())
        if details and isinstance(details[0], dict):
            ident = details[0]
            node = ident.get("node")
            pod = ident.get("pod")
            started_at = ident.get("started_at")
            last_heartbeat_at = ident.get("heartbeat_at")

    if not (node or pod):
        node, pod = _parse_worker_identity(getattr(err, "identity", None))

    return ActivityLocation(
        node=node, pod=pod, started_at=started_at, last_heartbeat_at=last_heartbeat_at
    )


def _parse_worker_identity(identity: str | None) -> tuple[str | None, str | None]:
    """Parse ``"{pod}@{node}"`` worker identity → ``(node, pod)``.

    Returns ``(None, None)`` when unset or not in that shape. The SDK only sets
    this form when configured to encode pod/node into the Temporal worker
    identity (see docs → "Identity carriers").
    """
    if not identity or "@" not in identity:
        return None, None
    pod, _, node = identity.partition("@")
    return (node or None), (pod or None)


# ---------------------------------------------------------------------------
# workflow-side: classification (YOUR policy) + recording
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RecordedCause:
    """How an infra verdict is recorded against the failure.

    Fields mirror the SDK taxonomy (:mod:`application_sdk.errors`) so downstream
    consumers (Automation Engine, SLA dashboards) route on the vocabulary they
    already use. ``infra_reason`` is the raw verdict, kept for humans.
    """

    code: str
    """``AppError.code`` — e.g. ``"RESOURCE_EXHAUSTED"``."""
    category: str
    """``FailureCategory`` value — e.g. ``"RESOURCE_EXHAUSTED"``."""
    audience: str
    """``Audience`` value — e.g. ``"PLATFORM"``."""
    retryable: bool
    infra_reason: str
    """The raw :class:`InfraCause` value — e.g. ``"OOM_KILLED"``."""


def classify_infra_failure(verdict: InfraVerdict) -> RecordedCause:
    """Map an infra-event verdict onto the SDK failure taxonomy.

    ─── YOUR CALL — the one piece of real judgment in this feature ─────────────
    Every branch is a policy decision, not a mechanical one. Fill each with the
    ``code`` / ``category`` / ``audience`` / ``retryable`` matching how on-call
    and the Automation Engine should treat it. Natural leaf classes from
    ``application_sdk.errors.leaves`` are noted — swap them if your ownership
    model differs.

    * ``OOM_KILLED`` — app team's problem (a leak → ``APP_OWNER``, perhaps
      ``retryable=False`` so attribution flags "don't just re-run") or
      platform's (undersized limits → ``PLATFORM``)? ``ResourceExhaustedError``
      is the natural leaf (``RESOURCE_EXHAUSTED`` / ``PLATFORM`` / retryable).
      You scoped OOM to *record only*, so ``retryable`` here labels intent, it
      does not drive a retry in Phase 1.
    * ``SPOT_RECLAIM`` / ``NODE_LOST`` — infra, not the app's fault.
      ``DependencyUnavailableError`` fits (``DEPENDENCY_UNAVAILABLE`` /
      ``PLATFORM`` / retryable).
    * ``NONE`` — service found nothing. The honest label is "timed out, cause
      unknown": ``AppTimeoutError`` (``TIMEOUT`` / ``APP_OWNER`` / retryable).
    ────────────────────────────────────────────────────────────────────────────

    Must stay pure (runs in workflow context): no I/O, no clock, no randomness.
    """
    # TODO(you): replace this catch-all stub with the per-cause mapping above
    # (≈5–10 lines, e.g. a dict keyed on ``verdict.cause``).
    return RecordedCause(
        code="TIMEOUT",
        category="TIMEOUT",
        audience="APP_OWNER",
        retryable=True,
        infra_reason=verdict.cause.value,
    )


async def _diagnose_and_record(err: ActivityError) -> None:
    """Diagnose the infra cause of a heartbeat timeout and record it.

    Best-effort: any failure here is logged and swallowed so the *original*
    activity failure always propagates unchanged. This never turns an
    attribution attempt into a second, masking failure.
    """
    from datetime import timedelta  # noqa: PLC0415 — keep workflow module top clean

    from temporalio.common import RetryPolicy  # noqa: PLC0415

    from application_sdk.constants import (  # noqa: PLC0415
        INFRA_DIAGNOSIS_TIMEOUT_SECONDS,
    )

    try:
        location = _decode_activity_location(err)
        verdict: InfraVerdict = await workflow.execute_activity(
            DIAGNOSE_ACTIVITY_NAME,
            args=[location],
            start_to_close_timeout=timedelta(seconds=INFRA_DIAGNOSIS_TIMEOUT_SECONDS),
            # Small retry with backoff: infra signals (Karpenter events, pod
            # lastState) often lag the heartbeat timeout by seconds.
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2.0,
            ),
            result_type=InfraVerdict,
        )
        cause = classify_infra_failure(verdict)
        workflow.upsert_search_attributes(
            [_INFRA_CAUSE_KEY.value_set(cause.infra_reason)]
        )
        # workflow.logger is a LoggerAdapter — custom fields must nest under
        # ``extra`` (see eviction_retry.py for the same constraint).
        workflow.logger.warning(
            "activity failed on heartbeat timeout; diagnosed infra cause=%s "
            "(code=%s audience=%s pod=%s node=%s)",
            cause.infra_reason,
            cause.code,
            cause.audience,
            location.pod,
            location.node,
            extra={
                "infra_reason": cause.infra_reason,
                "failure_code": cause.code,
                "failure_audience": cause.audience,
                "failure_retryable": cause.retryable,
                "node": location.node,
                "pod": location.pod,
            },
        )
    except Exception:
        workflow.logger.warning(
            "infra diagnosis/record failed; original activity failure "
            "propagates unchanged",
            exc_info=True,
        )


async def execute_activity_with_infra_diagnosis(*args: Any, **kwargs: Any) -> Any:
    """Drop-in for :func:`execute_activity_with_eviction_retry` that also records
    an infra cause when an activity dies on a heartbeat timeout.

    When ``ENABLE_INFRA_FAILURE_DIAGNOSIS`` is off this is a pure passthrough —
    byte-for-byte the eviction-retry behaviour, no extra activity, no extra
    workflow history. That keeps the feature safe to wire in before the
    infra-event service exists.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — read at call time so the flag is togglable
        ENABLE_INFRA_FAILURE_DIAGNOSIS,
    )

    try:
        return await execute_activity_with_eviction_retry(*args, **kwargs)
    except ActivityError as err:
        if ENABLE_INFRA_FAILURE_DIAGNOSIS and _is_heartbeat_timeout(err):
            await _diagnose_and_record(err)
        raise


# ---------------------------------------------------------------------------
# activity-side: the diagnosis activity + identity seeding
# ---------------------------------------------------------------------------


@activity.defn(name=DIAGNOSE_ACTIVITY_NAME)
async def diagnose_infra_failure(location: ActivityLocation) -> InfraVerdict:
    """Ask the infra-event service what happened to a stopped activity's pod.

    Runs as its own short activity (one HTTP call) so it may do I/O and so its
    result is cached across replay. Fails safe: any problem yields
    :meth:`InfraVerdict.none` — it never raises, so it cannot turn an
    attribution attempt into a second failure.
    """
    if not location.is_correlatable():
        return InfraVerdict.none("no node/pod to correlate")
    client = get_infra_event_client()
    try:
        return await client.lookup(location)
    except Exception:
        logger.warning(
            "infra-event lookup failed for pod=%s node=%s; recording NONE",
            location.pod,
            location.node,
            exc_info=True,
        )
        return InfraVerdict.none("lookup raised")


def read_pod_identity() -> dict[str, str]:
    """Read pod/node identity from the Kubernetes downward API env.

    Requires the pod spec to expose ``NODE_NAME`` / ``POD_NAME`` via
    ``fieldRef`` (``spec.nodeName`` / ``metadata.name``). ``HOSTNAME`` is used
    as a pod-name fallback (equals the pod name in Kubernetes). Missing values
    are omitted; diagnosis then falls back to worker identity.
    """
    import os  # noqa: PLC0415 — activity-side only; keep os out of the workflow module top

    ident: dict[str, str] = {}
    node = os.getenv("NODE_NAME")
    pod = os.getenv("POD_NAME") or os.getenv("HOSTNAME")
    if node:
        ident["node"] = node
    if pod:
        ident["pod"] = pod
    return ident


def seed_activity_identity(heartbeat_controller: Any) -> None:
    """Seed the heartbeat with pod/node identity so it survives into a timeout.

    Records ``{node, pod, started_at}`` as the first heartbeat detail; the
    auto-heartbeat keepalive re-sends the same details, so a later
    ``TimeoutError.last_heartbeat_details`` carries the identity after an abrupt
    kill. No-op when the feature is disabled or identity env is absent.

    Known limitation: a task that later calls ``heartbeat(progress)`` manually
    overwrites these details (see ``HeartbeatController.get_last_heartbeat_details``
    semantics). For those tasks, worker identity (``ActivityError.identity``) is
    the fallback carrier — see docs → "Identity carriers".
    """
    from application_sdk.constants import (  # noqa: PLC0415
        ENABLE_INFRA_FAILURE_DIAGNOSIS,
    )

    if not ENABLE_INFRA_FAILURE_DIAGNOSIS:
        return
    identity = read_pod_identity()
    if not identity:
        return

    from datetime import (  # noqa: PLC0415 — activity-side clock is fine
        datetime,
        timezone,
    )

    identity["started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        heartbeat_controller.heartbeat(identity)
    except Exception:
        logger.debug("failed to seed activity identity heartbeat", exc_info=True)
