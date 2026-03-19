"""Pytest fixtures for testing Apps.

Import these fixtures in your conftest.py or test files::

    from application_sdk.testing.fixtures import app_context, mock_state_store

Or import everything via the testing module::

    from application_sdk.testing import app_context, mock_state_store
"""

from collections.abc import Generator

import pytest

from application_sdk.app.context import AppContext
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.test_utils.credentials import MockCredentialStore
from application_sdk.testing.mocks import (
    MockBinding,
    MockHeartbeatController,
    MockPubSub,
    MockSecretStore,
    MockStateStore,
)


@pytest.fixture
def mock_state_store() -> MockStateStore:
    """Fresh MockStateStore instance."""
    return MockStateStore()


@pytest.fixture
def mock_secret_store() -> MockSecretStore:
    """Fresh MockSecretStore instance."""
    return MockSecretStore()


@pytest.fixture
def mock_pubsub() -> MockPubSub:
    """Fresh MockPubSub instance."""
    return MockPubSub()


@pytest.fixture
def mock_binding() -> MockBinding:
    """Fresh MockBinding instance."""
    return MockBinding()


@pytest.fixture
def mock_heartbeat() -> MockHeartbeatController:
    """Fresh MockHeartbeatController instance."""
    return MockHeartbeatController()


@pytest.fixture
def mock_credential_store() -> MockCredentialStore:
    """Fresh MockCredentialStore instance."""
    return MockCredentialStore()


@pytest.fixture
def app_context(
    mock_state_store: MockStateStore,
    mock_secret_store: MockSecretStore,
) -> AppContext:
    """AppContext wired with MockStateStore and MockSecretStore."""
    return AppContext(
        app_name="test-app",
        app_version="0.0.1",
        _state_store=mock_state_store,
        _secret_store=mock_secret_store,
    )


@pytest.fixture
def clean_app_registry() -> Generator[AppRegistry, None, None]:
    """AppRegistry reset before and after each test."""
    AppRegistry.reset()
    registry = AppRegistry.get_instance()
    yield registry
    AppRegistry.reset()


@pytest.fixture
def clean_task_registry() -> Generator[TaskRegistry, None, None]:
    """TaskRegistry reset before and after each test."""
    TaskRegistry.reset()
    registry = TaskRegistry.get_instance()
    yield registry
    TaskRegistry.reset()
