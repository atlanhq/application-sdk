"""Typed error leaves for the full-DAG end-to-end test harness."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, ClassVar

from application_sdk.errors.leaves import (
    AppTimeoutError,
    DataIntegrityError,
    DependencyUnavailableError,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
    UnimplementedError,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    # client imports this module, so the reverse import is deferred.
    from application_sdk.testing.e2e.client import DAGRunResult

# ---------------------------------------------------------------------------
# Atlan API client errors (Family B)
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class AtlanApiHttpError(DependencyUnavailableError):
    """Non-2xx response from the Atlan Automation Engine API.

    ``retry_after_seconds`` carries the origin's own backoff request when the
    response body supplied one (``{"retryable": true, "retry_after": 120}``).
    Named to match :class:`~application_sdk.errors.leaves.RateLimitedError`'s
    field of the same meaning. Retry loops honour it instead of their fixed
    gap; on a terminal raise it stays attached so the operator can see the
    wait the origin asked for versus the budget the harness had.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_ATLAN_API"
    service: str | None = "atlan_api"
    retry_after_seconds: float | None = None


@dataclass(kw_only=True)
class AtlanApiResponseInvariantError(DataIntegrityError):
    """AE API returned 2xx but the expected field (slug, run_id) was absent."""

    code: ClassVar[str] = "DATA_INTEGRITY_ATLAN_API_RESPONSE"
    location: str | None = "atlan_api_client"


class RequestDelivery(str, Enum):
    """Whether a failed HTTP request can have taken effect at the origin.

    The distinction only matters for a **non-idempotent** write (the AE
    submit): re-issuing one the origin already processed spawns a duplicate
    run, so the client must know whether that is possible before retrying.

    Two independent signals narrow the ambiguous case:

    * the transport error's own shape. ``urllib`` could not tell a connect
      timeout from a read timeout — both surfaced as a bare
      :class:`TimeoutError` — which forced the submit path to treat every
      network failure as ambiguous and give up after one attempt. ``httpx``
      separates the connect phase from everything after it, giving
      :attr:`NOT_DELIVERED`.
    * a follow-up read of the origin's own state. A write whose effect is
      externally observable can simply be looked up afterwards, giving
      :attr:`NOT_APPLIED` when it is provably absent.
    """

    NOT_DELIVERED = "not_delivered"
    """The connection was never established, so the request bytes never left
    the client. The origin cannot have seen it — re-issuing is safe even for a
    non-idempotent write."""

    NOT_APPLIED = "not_applied"
    """The request may well have reached the origin, but a follow-up read of
    the origin's own state found no trace of its effect. Re-issuing is safe on
    the strength of that read, not of the transport error."""

    AMBIGUOUS = "ambiguous"
    """The connection was established, the failure came later (read timeout,
    reset mid-flight), and nothing has ruled out the origin having processed
    it. Only an idempotent caller may re-issue."""


@dataclass(kw_only=True)
class AtlanApiTimeoutError(AppTimeoutError):
    """No response received from the AE API before the timeout elapsed.

    ``delivery`` records whether the request can have reached the origin. It
    defaults to :attr:`RequestDelivery.AMBIGUOUS` — the conservative value —
    so any raise site that does not classify the failure keeps the old
    never-repost behaviour for non-idempotent writes.
    """

    code: ClassVar[str] = "TIMEOUT_ATLAN_API"
    operation: str | None = "native_status_poll"
    delivery: RequestDelivery = RequestDelivery.AMBIGUOUS


# ---------------------------------------------------------------------------
# Harness setup / precondition errors (Family C)
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class MissingHarnessClassAttrError(InvalidInputError):
    """A required class-level attribute was not set on the test harness."""

    code: ClassVar[str] = "INVALID_INPUT_HARNESS_CLASS_ATTR"


@dataclass(kw_only=True)
class MissingHarnessEnvError(InvalidInputError):
    """Required environment variables for the full-DAG harness are absent."""

    code: ClassVar[str] = "INVALID_INPUT_HARNESS_ENV"
    field: str | None = "ATLAN_BASE_URL,ATLAN_API_KEY"


@dataclass(kw_only=True)
class ManifestFileNotFoundError(NotFoundError):
    """The workflow manifest JSON file does not exist at the expected path."""

    code: ClassVar[str] = "NOT_FOUND_MANIFEST"
    resource_type: str | None = "manifest"


@dataclass(kw_only=True)
class ManifestDagMissingError(DataIntegrityError):
    """The manifest file exists but contains no top-level ``dag`` object."""

    code: ClassVar[str] = "DATA_INTEGRITY_MANIFEST_DAG_MISSING"
    expectation: str | None = "dag object present"


@dataclass(kw_only=True)
class DeployedManifestMismatchError(DataIntegrityError):
    """The DAG AE published at submit is not the DAG the app under test declares.

    The version check upstream of this one asserts the right *image* is
    installed. This asserts the right *graph* ran, which is a different claim:
    at submit, Heracles re-fetches the manifest from the tenant-deployed pod and
    calls ``CreateVersion`` + ``PublishVersion`` on the harness's own slug,
    superseding the seed version. So the graph that executes is the pod's, and
    until this check nothing named the difference — a leg whose assertions all
    pass could have been exercising a graph the repo never built.

    Raised only on a positive finding: the read got through, AE's published
    version provably superseded the harness's seed, and the node identities
    still disagree. Every unanswerable outcome (an unreadable response, a
    version that never superseded, a connector with no manifest to compare)
    logs and continues — see
    :meth:`~application_sdk.testing.e2e.base.BaseE2ETest._assert_deployed_manifest_matches`.

    ``observed`` carries the rendered node-set / per-node diff
    (:meth:`~application_sdk.testing.e2e._manifest_identity.ManifestIdentityDiff.render`)
    so the failure names which nodes diverged rather than only that some did.
    """

    code: ClassVar[str] = "DATA_INTEGRITY_DEPLOYED_MANIFEST_MISMATCH"
    expectation: str | None = (
        "the published DAG's node identities match the local manifest's"
    )


@dataclass(kw_only=True)
class AdminRoleNotResolvedError(PreconditionError):
    """The pyatlan role cache could not resolve the ``$admin`` role GUID."""

    code: ClassVar[str] = "PRECONDITION_ADMIN_ROLE_NOT_RESOLVED"
    expected_state: str | None = "role $admin present in role cache"


@dataclass(kw_only=True)
class AppNotReadyError(PreconditionError):
    """The tenant-installed app pod did not accept connections before AE submit.

    ``prepare-tenant`` installs the app and LM reports the deployment
    reconciled, but the pod can still be tens of seconds away from serving HTTP
    on ``:8000`` when the leg reaches the AE submit. At submit, Heracles POSTs
    the credential config to
    ``http://<conn>.<conn>-app.svc.cluster.local:8000/workflows/v1/config/...``
    against that pod; a not-yet-serving pod surfaces as
    ``AE submit failed: HTTP 500 ... dial tcp :8000: connect: connection
    refused``.

    Raised by :meth:`AEWorkflowClient.submit_workflow` only when its retry
    budget is exhausted *and* the last response still read as connection-
    refused (:func:`_is_app_not_ready`) — i.e. the terminal form of the race,
    so the failure names "app never became ready" instead of an opaque 500. A
    race that resolves within the budget returns a run_id and never reaches
    this type. This mirrors
    :class:`~application_sdk.errors.leaves.DaprSidecarUnreachableError`, the
    same "waited the whole cold-start budget, done waiting" signal for the Dapr
    sidecar, and carries the same ``attempts`` / ``elapsed_seconds`` fields so
    the budget that expired is a queryable field rather than only prose.
    """

    code: ClassVar[str] = "PRECONDITION_APP_NOT_READY"
    expected_state: str | None = "tenant app pod serving HTTP on :8000"
    actual_state: str | None = "refusing connections on :8000"
    attempts: int | None = None
    elapsed_seconds: float | None = None


@dataclass(kw_only=True)
class NoWorkerOnTaskQueueError(PreconditionError):
    """No worker started any DAG node while the AE run was live.

    Raised only when the top-level run has left ``Pending`` — the parent
    workflow runs on the always-on automation-engine queue, so a live parent
    means AE dispatched and the connector's own node is the one stuck. That
    points at the app: an agent-name / task-queue mismatch, where the test's
    ``agent_spec().agent_name`` must resolve to the same queue the deployed
    worker polls (``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}``).
    Rather than hang for the full ``ae_poll_timeout_seconds`` (often 30 min),
    the harness fails fast here.

    A top-level run still sitting ``Pending`` is :class:`AutomationEngineNotDispatchingError`
    instead — nothing reached the app, so the app's queue is not implicated.
    """

    code: ClassVar[str] = "PRECONDITION_NO_WORKER_ON_TASK_QUEUE"
    expected_state: str | None = "a worker polling the extract task queue"


@dataclass(kw_only=True)
class AutomationEngineNotDispatchingError(PreconditionError):
    """The AE run never left ``Pending`` within the stall-grace window.

    The top-level status is the Automation Engine's own state. While it is
    ``Pending`` the run has not been dispatched at all, so no DAG node could
    have started and nothing has yet been offered to the connector's task
    queue. The app, its worker and its agent name are therefore not implicated
    — attributing this to a missing worker sends triage to the wrong system.
    The usual causes are tenant-side: AE contention on a shared e2e tenant, or
    an AE worker that is not processing new runs.
    """

    code: ClassVar[str] = "PRECONDITION_AE_NOT_DISPATCHING"
    expected_state: str | None = "the AE run leaving Pending"


@dataclass(kw_only=True)
class DAGProgressStalledError(PreconditionError):
    """A DAG node ran without any state transition for the progress window.

    Distinct from :class:`NoWorkerOnTaskQueueError`, which guards the *start*
    (no node ever leaves ``Pending``). This guards *forward progress*: a node
    that has begun but sits ``Running`` — with no node in the DAG changing state
    — for ``dag_progress_stall_seconds`` almost always means it is wedged (e.g.
    an extract stuck on a slow/failing upload). Rather than let the harness poll
    the full ``ae_poll_timeout_seconds`` (often 90 min) and require a manual
    cancel, we fail fast with the last-seen node states so the wedge is visible.
    The window is set comfortably above legitimately slow single nodes (lineage
    on deep queues can sit Running for many minutes), so a healthy run never
    trips it.

    ``result`` carries the last observation the poll made, stamped with
    ``progress_stalled_after_seconds``. Raising bare left this path with only a
    ``name=status`` list, so the caller that owns the diagnostic renderer (it
    needs the seed DAG's routing, which the client does not have) could not
    name the task queue or the child workflow to read. Attaching the typed
    result lets that one renderer serve the watchdog stop and the poll ceiling
    alike.
    """

    code: ClassVar[str] = "PRECONDITION_DAG_PROGRESS_STALLED"
    expected_state: str | None = (
        "at least one DAG node state transition within the progress window"
    )
    result: DAGRunResult | None = None


@dataclass(kw_only=True)
class ProgressWatchdogUnreachableError(InvalidInputError):
    """``dag_progress_stall_seconds`` is pinned at or above the poll ceiling.

    The watchdog fires when ``elapsed - last_progress_elapsed`` reaches the
    window, and ``poll_native_status`` returns as soon as ``elapsed`` reaches
    ``ae_poll_timeout_seconds``. A window that is not strictly smaller than the
    ceiling can therefore only ever close on a run that stalls at t=0 — for
    every real stall the poll loop exits first, so the suite burns its whole
    ceiling and reports the ceiling instead of the stall.

    Raised at ``setup_method`` rather than warned about: the configuration
    silently disables a fail-fast guard, and the only way to notice at runtime
    is to read both numbers and do the subtraction. Leave
    ``dag_progress_stall_seconds`` unset to derive a window from the ceiling, or
    set 0 to disable the watchdog deliberately.
    """

    code: ClassVar[str] = "INVALID_INPUT_PROGRESS_WATCHDOG_UNREACHABLE"
    field: str | None = "dag_progress_stall_seconds"
    constraint: str | None = "must be < ae_poll_timeout_seconds"


@dataclass(kw_only=True)
class AtlanAEWorkflowAlreadyActiveError(PreconditionError):
    """A run for the AE workflow is already active, so a new submit was rejected.

    AE returns ``AE-WF-409-03`` ("a run for workflow '<slug>' is already
    active") when a submit collides with an in-flight run — but Heracles (the
    tenant-facing proxy in front of Automation Engine) masks it as an HTTP 500
    with the original 409 text embedded.

    This is a *terminal* state-conflict, not a recoverable dependency blip: the
    same submit cannot succeed until the active run ends. It is therefore a
    non-retryable ``PreconditionError`` (like the sibling
    ``NoWorkerOnTaskQueueError``) rather than a retryable
    ``DependencyUnavailableError``. Retrying a non-idempotent submit spawns a
    duplicate run that AE marks ``Skipped``, which the test then mistracks
    (surfacing as a spurious ``NoWorkerOnTaskQueueError``). Its run_id is
    unrecoverable via native-status (keyed by run_id), so we fail fast with the
    true cause.
    """

    code: ClassVar[str] = "PRECONDITION_AE_WORKFLOW_ALREADY_ACTIVE"
    expected_state: str | None = "no active run for this workflow"


@dataclass(kw_only=True)
class UnknownConnectorTypeError(InvalidInputError):
    """The suite's connection type is not a pyatlan ``AtlanConnectorType``.

    Raised by :meth:`~application_sdk.testing.e2e.base.BaseE2ETest.seed_connection`,
    which needs a real connector type to create the Connection. The harness's
    own ``connection_type or connector_short_name`` fallback is fine for
    composing a qualifiedName segment but not for this, because an app name and
    an Atlan catalog type legitimately differ (the OpenAPI connector is
    ``connector_short_name="openapi"`` / ``connection_type="api"``). Failing
    here names the fix rather than surfacing a bare pyatlan ``ValueError``.
    """

    code: ClassVar[str] = "INVALID_INPUT_UNKNOWN_CONNECTOR_TYPE"
    field: str | None = "connection_type"


@dataclass(kw_only=True)
class SeededConnectionNotSearchableError(PreconditionError):
    """A seeded Connection never became searchable within the poll window.

    :meth:`~application_sdk.testing.e2e.base.BaseE2ETest.seed_connection` saved
    the Connection but Atlas never returned it. Running the DAG anyway would
    exercise the entrypoint against a connection the platform cannot see, so
    this fails fast instead — a wedged seed is a harness precondition failure,
    not a connector defect, and conflating the two sends the investigation to
    the wrong team.
    """

    code: ClassVar[str] = "PRECONDITION_SEEDED_CONNECTION_NOT_SEARCHABLE"
    expected_state: str | None = "seeded connection searchable in Atlas"


@dataclass(kw_only=True)
class AgentSpecRequiredError(InvalidInputError):
    """Agent mode requires an ``AgentSpec`` but none was provided."""

    code: ClassVar[str] = "INVALID_INPUT_AGENT_SPEC_REQUIRED"
    message: str = "Agent mode requires an AgentSpec"
    field: str | None = "agent_spec"
    constraint: str | None = "required_for_agent_mode"


@dataclass(kw_only=True)
class HarnessMethodNotImplementedError(UnimplementedError):
    """Abstract test harness method was not overridden by a connector subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_HARNESS_METHOD"
    message: str = "Test harness subclass did not implement required method"
    component: str | None = "e2e_harness"
