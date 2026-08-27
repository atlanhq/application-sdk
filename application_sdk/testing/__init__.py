"""Testing utilities for application_sdk Apps.

This module provides mock infrastructure implementations with call-tracking
and pytest fixtures, so tests never need to import temporalio directly.

Usage::

    from application_sdk.testing import MockStateStore, app_context, MockCredentialStore

For an HTTP source with no container to pull, :class:`HttpFakeSource` fills the
same fixture slot a testcontainer would::

    from application_sdk.testing import HttpFakeSource, offset_page

Fixtures (import into conftest.py or test files)::

    from application_sdk.testing import (
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
    )
"""

from application_sdk.testing.fake_source import (
    CursorPage,
    FakeRequest,
    FakeResponse,
    FakeSourceGroup,
    HttpFakeSource,
    HttpFakeSourceFactory,
    OffsetPage,
    assert_extract_roundtrip,
    cursor_page,
    offset_page,
    serve,
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
)
from application_sdk.testing.mocks import (
    MockBinding,
    MockCredentialStore,
    MockHeartbeatController,
    MockPubSub,
    MockSecretStore,
    MockStateStore,
)

__all__ = [
    "CursorPage",
    "FakeRequest",
    "FakeResponse",
    "FakeSourceGroup",
    "HttpFakeSource",
    "HttpFakeSourceFactory",
    "MockBinding",
    "MockCredentialStore",
    "MockHeartbeatController",
    "MockPubSub",
    "MockSecretStore",
    "MockStateStore",
    "OffsetPage",
    "app_context",
    "assert_extract_roundtrip",
    "clean_app_registry",
    "clean_http_fake_sources",
    "clean_task_registry",
    "cursor_page",
    "http_fake_source_factory",
    "mock_binding",
    "mock_credential_store",
    "mock_heartbeat",
    "mock_pubsub",
    "mock_secret_store",
    "mock_state_store",
    "offset_page",
    "serve",
]
