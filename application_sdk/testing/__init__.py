"""Testing utilities for application_sdk Apps.

This module provides mock infrastructure implementations with call-tracking
and pytest fixtures, so tests never need to import temporalio directly.

Usage::

    from application_sdk.testing import MockStateStore, app_context, MockCredentialStore

Fixtures (import into conftest.py or test files)::

    from application_sdk.testing import (
        app_context,
        clean_app_registry,
        clean_task_registry,
        mock_binding,
        mock_credential_store,
        mock_heartbeat,
        mock_pubsub,
        mock_secret_store,
        mock_state_store,
    )
"""

from application_sdk.test_utils.credentials import MockCredentialStore
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
)
from application_sdk.testing.mocks import (
    MockBinding,
    MockHeartbeatController,
    MockPubSub,
    MockSecretStore,
    MockStateStore,
)

__all__ = [
    # Mocks
    "MockBinding",
    "MockCredentialStore",
    "MockHeartbeatController",
    "MockPubSub",
    "MockSecretStore",
    "MockStateStore",
    # Fixtures
    "app_context",
    "clean_app_registry",
    "clean_task_registry",
    "mock_binding",
    "mock_credential_store",
    "mock_heartbeat",
    "mock_pubsub",
    "mock_secret_store",
    "mock_state_store",
]
