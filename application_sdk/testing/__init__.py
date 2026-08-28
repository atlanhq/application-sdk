"""Testing utilities for application_sdk Apps.

This module provides mock infrastructure implementations with call-tracking
and pytest fixtures, so tests never need to import temporalio directly.

Usage::

    from application_sdk.testing import MockStateStore, app_context, MockCredentialStore

For an HTTP source with no container to pull, :class:`HttpFakeSource` fills the
same fixture slot a testcontainer would::

    from application_sdk.testing import HttpFakeSource

Fixtures (import into conftest.py or test files)::

    from application_sdk.testing import (
        reset_http_fake_sources,
        app_context,
        clean_app_registry,
        clean_http_fake_sources,
        clean_task_registry,
        http_fake_source_factory,
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
    clean_http_fake_sources,
    clean_task_registry,
    http_fake_source_factory,
    mock_binding,
    mock_credential_store,
    mock_heartbeat,
    mock_pubsub,
    mock_secret_store,
    mock_state_store,
    reset_http_fake_sources,
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
    "app_context",
    "clean_app_registry",
    "clean_http_fake_sources",
    "clean_task_registry",
    "http_fake_source_factory",
    "mock_binding",
    "mock_credential_store",
    "mock_heartbeat",
    "mock_pubsub",
    "mock_secret_store",
    "mock_state_store",
    "reset_http_fake_sources",
    "restore_logger_init_flags",
]
