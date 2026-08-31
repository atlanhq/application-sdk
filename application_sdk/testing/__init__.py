"""Testing utilities for application_sdk Apps.

This module provides mock infrastructure implementations with call-tracking
and pytest fixtures, so tests never need to import temporalio directly.

Usage::

    from application_sdk.testing import MockStateStore, app_context, MockCredentialStore

For an HTTP source with no container to pull, :class:`HttpFakeSource` fills the
same fixture slot a testcontainer would::

    from application_sdk.testing import HttpFakeSource

Its pytest fixtures are part of the integration kit rather than this module, so
one star-import gives a connector the factory and its autouse reset together::

    from application_sdk.testing.integration.fixtures import *  # noqa: F403

To assert what the preflight gate decided, read its outcome row through the
shared capture rather than patching the gate's logger by hand — the row is
level-carried, so an ``info``-only reader goes blind when a verdict changes
level::

    from application_sdk.testing import capture_preflight_outcomes

Fixtures (import into conftest.py or test files)::

    from application_sdk.testing import (
        app_context,
        capture_preflight_outcomes,
        clean_app_registry,
        clean_task_registry,
        mock_binding,
        mock_credential_store,
        mock_heartbeat,
        mock_pubsub,
        mock_secret_store,
        mock_state_store,
        restore_logger_init_flags,
    )
"""

from application_sdk.testing.fake_source import (
    Authorizer,
    FakeRequest,
    FakeResponse,
    Handler,
    HandlerResult,
    HttpFakeSource,
    HttpFakeSourceFactory,
)
from application_sdk.testing.fixtures import (
    app_context,
    clean_app_registry,
    clean_task_registry,
    mock_binding,
    mock_credential_store,
    mock_heartbeat,
    mock_pubsub,
    mock_secret_store,
    mock_state_store,
    restore_logger_init_flags,
)
from application_sdk.testing.mocks import (
    MockBinding,
    MockCredentialStore,
    MockHeartbeatController,
    MockPubSub,
    MockSecretStore,
    MockStateStore,
)
from application_sdk.testing.preflight import (
    OUTCOME_LEVELS,
    PreflightOutcomeCapture,
    capture_preflight_outcomes,
    first_outcome_or_none,
    outcome_level,
    outcome_rows,
    single_outcome,
)
from application_sdk.testing.volatile_fields import (
    ENVIRONMENT_SCOPED_FIELDS,
    ENVIRONMENT_SCOPED_NESTED_FIELDS,
    RUN_VOLATILE_FIELDS,
)

__all__ = [
    "ENVIRONMENT_SCOPED_FIELDS",
    "ENVIRONMENT_SCOPED_NESTED_FIELDS",
    "RUN_VOLATILE_FIELDS",
    "Authorizer",
    "FakeRequest",
    "FakeResponse",
    "Handler",
    "HandlerResult",
    "HttpFakeSource",
    "HttpFakeSourceFactory",
    "MockBinding",
    "MockCredentialStore",
    "MockHeartbeatController",
    "MockPubSub",
    "MockSecretStore",
    "MockStateStore",
    "OUTCOME_LEVELS",
    "PreflightOutcomeCapture",
    "app_context",
    "capture_preflight_outcomes",
    "clean_app_registry",
    "clean_task_registry",
    "first_outcome_or_none",
    "mock_binding",
    "mock_credential_store",
    "mock_heartbeat",
    "mock_pubsub",
    "mock_secret_store",
    "mock_state_store",
    "outcome_level",
    "outcome_rows",
    "restore_logger_init_flags",
    "single_outcome",
]
