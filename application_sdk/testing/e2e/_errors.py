"""Typed error leaves for the full-DAG end-to-end test harness.

Ten of these moved into the harness with the AE half of ``client.py`` (child F
on FND-224) and are re-exported below: nine to
:mod:`application_sdk.testing.harness.automation_engine._errors` because the AE
reader raises them, and ``MissingHarnessClassAttrError`` to
:mod:`application_sdk.testing.harness._errors` because ``cold_start_submit_kwargs``
does. Same class objects and same ``code`` values, so every existing import and
``except`` clause here is unchanged; the move is only about direction, since a
harness module cannot raise a leaf that lives in the package child H
re-expresses over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DataIntegrityError,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
    UnimplementedError,
)
from application_sdk.testing.harness._errors import MissingHarnessClassAttrError
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
    "AgentSpecRequiredError",
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
