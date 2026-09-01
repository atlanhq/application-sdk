"""Typed error leaves for the full-DAG end-to-end test harness.

Eleven of these have moved into the harness and are re-exported below. Ten went
with the AE half of ``client.py`` (child F on FND-224): nine to
:mod:`application_sdk.testing.harness.automation_engine._errors` because the AE
reader raises them, and ``MissingHarnessClassAttrError`` to
:mod:`application_sdk.testing.harness._errors` because ``cold_start_submit_kwargs``
does. ``UnknownConnectorTypeError`` went to
:mod:`application_sdk.testing.harness.atlas._errors` in child H, because
``create_connection`` is what raises it now. Same class objects and same ``code``
values, so every existing import and ``except`` clause here is unchanged; the
move is only about direction, since a harness module cannot raise a leaf that
lives in the package child H re-expresses over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DataIntegrityError,
    DependencyUnavailableError,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
    UnimplementedError,
)
from application_sdk.testing.harness._errors import MissingHarnessClassAttrError
from application_sdk.testing.harness.atlas._errors import UnknownConnectorTypeError
from application_sdk.testing.harness.automation_engine._errors import (
    AppNotReadyError,
    AtlanAEWorkflowAlreadyActiveError,
    AtlanApiHttpError,
    AtlanApiResponseInvariantError,
    AtlanApiTimeoutError,
    AutomationEngineNotDispatchingError,
    DAGProgressStalledError,
    NoWorkerOnTaskQueueError,
    RequestDelivery,
)

__all__ = [
    "AdminRoleNotResolvedError",
    "AmbiguousDAGRunError",
    "AgentSpecRequiredError",
    "AtlasReadIndeterminateError",
    "AppNotReadyError",
    "AtlanAEWorkflowAlreadyActiveError",
    "AtlanApiHttpError",
    "AtlanApiResponseInvariantError",
    "AtlanApiTimeoutError",
    "AutomationEngineNotDispatchingError",
    "DAGProgressStalledError",
    "DeployedManifestMismatchError",
    "HarnessMethodNotImplementedError",
    "ManifestDagMissingError",
    "ManifestFileNotFoundError",
    "MissingHarnessClassAttrError",
    "MissingHarnessEnvError",
    "NoWorkerOnTaskQueueError",
    "ProgressWatchdogUnreachableError",
    "RequestDelivery",
    "SeededConnectionNotSearchableError",
    "UnknownConnectorTypeError",
    "WorkerNotHealthyError",
]

# ---------------------------------------------------------------------------
# Harness setup / precondition errors (Family C)
# ---------------------------------------------------------------------------


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
class AmbiguousDAGRunError(InvalidInputError):
    """A suite's ``dag_runs`` declaration cannot resolve to distinct runs.

    Either two runs resolve to the same label without being the same run — the
    label names the AE workflow each run seeds, so a collision would publish one
    run's DAG over the other's — or the suite pins ``ae_workflow_slug`` while
    declaring several runs, which cannot be honoured because a pinned slug is
    submitted against as-is and never seeded over.

    Static, so it is raised from ``setup_method`` rather than discovered as a
    confusing AE run list halfway through a leg.
    """

    code: ClassVar[str] = "INVALID_INPUT_AMBIGUOUS_DAG_RUN"


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
class AtlasReadIndeterminateError(DependencyUnavailableError):
    """An Atlas read the run is graded on could not be taken at all.

    Not an ``AssertionError``, and that is the whole point of the leaf. Before
    child H a failed Atlas search arrived at the assertion ladder as zeros, so an
    expired token or a 503 was reported as "the connector landed no assets" — a
    confident claim about the thing under test, made by a run that never read
    it. The harness readers answer
    :class:`~application_sdk.testing.harness.outcome.Indeterminate` for that
    case, and this is what
    :meth:`~application_sdk.testing.e2e.base.BaseE2ETest._assert_full_dag_outcome`
    raises when it sees one: pytest reports an *error* rather than a failure, so
    a red leg cannot be read as a connector regression.

    ``DEPENDENCY_UNAVAILABLE`` for the reason
    :class:`~application_sdk.testing.harness._errors.WaitIndeterminateError`
    carries it: the same call is expected to work once Atlas recovers, which is
    the category's own litmus test.

    Attributes:
        checks: Comma-separated names of the expectations that went ungraded.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_ATLAS_READ_INDETERMINATE"
    component: str | None = "e2e_harness_atlas"
    checks: str | None = None


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
class WorkerNotHealthyError(PreconditionError, AssertionError):
    """The app worker never served a 2xx from ``/server/health``.

    The no-source tier's whole verdict. When a connector has no extraction source
    in CI the full DAG cannot run, so
    :meth:`~application_sdk.testing.e2e.base.BaseE2ETest.assert_worker_up` proves
    the worker deployed instead — and an unhealthy worker must fail RED rather
    than be reported as the skip that a *healthy* one earns.

    ``PreconditionError`` for the reason
    :class:`SeededConnectionNotSearchableError` is one: the budget did not run
    out on work that was progressing, the state that had to exist before any work
    could begin never did. Retrying without changing that state is not expected
    to help.

    **Also an** :class:`AssertionError`, deliberately. That is not a hedge and it
    is not for pytest's benefit — pytest reds on any exception. It is because
    ``assert_worker_up``'s docstring has promised an ``AssertionError`` since the
    method existed, and out-of-repo connector suites are entitled to have written
    ``except AssertionError`` against it. Typing the leaf is worth doing; taking a
    documented ``except`` clause away from every connector in the fleet to do it
    is not, and the two are not in tension — this raise satisfies both.

    Attributes:
        url: The health endpoint that never answered 2xx.
        attempts: How many probes were made.
        elapsed_seconds: Wall-clock time the wait consumed.
        last_error: The last failure seen — an ``HTTP <code>`` or a transport
            error's own message. The single most useful field: a refused
            connection and a 503 point at different halves of a deployment.
    """

    code: ClassVar[str] = "PRECONDITION_WORKER_NOT_HEALTHY"
    expected_state: str | None = "app worker serving 2xx from /server/health"
    url: str | None = None
    attempts: int | None = None
    elapsed_seconds: float | None = None
    last_error: str | None = None


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
