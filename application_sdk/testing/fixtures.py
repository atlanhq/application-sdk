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
from application_sdk.constants import LOCAL_WORKFLOW_ID
from application_sdk.testing.fake_source import HttpFakeSourceFactory
from application_sdk.testing.mocks import (
    MockBinding,
    MockCredentialStore,
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
        workflow_id=LOCAL_WORKFLOW_ID,
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


@pytest.fixture(scope="session")
def http_fake_source_factory() -> Generator[HttpFakeSourceFactory, None, None]:
    """Session-scoped factory for started HttpFakeSource servers.

    The slot a testcontainer fixture occupies, for an HTTP source with no image to
    pull. Call it from the connector's own session-scoped fixture to build a fake,
    register that connector's routes on the returned instance, and yield its
    ``base_url``; every fake built here is stopped when the session ends::

        @pytest.fixture(scope="session")
        def source_url(http_fake_source_factory) -> str:
            fake = http_fake_source_factory(name="my-source")
            fake.route(r"/api/v1/objects", list_objects)
            return fake.base_url

    Pair with ``clean_http_fake_sources`` so per-test assertions on recorded
    requests are not reading a previous test's traffic.
    """
    factory = HttpFakeSourceFactory()
    try:
        yield factory
    finally:
        factory.stop_all()


@pytest.fixture
def clean_http_fake_sources(
    http_fake_source_factory: HttpFakeSourceFactory,
) -> Generator[HttpFakeSourceFactory, None, None]:
    """Recorded requests and route hits reset before and after each test.

    Function-scoped counterpart to ``http_fake_source_factory``: the servers stay
    up across the session, while ``requests``, ``unmatched`` and route hits start
    empty in every test that asks for this.
    """
    http_fake_source_factory.reset_all()
    yield http_fake_source_factory
    http_fake_source_factory.reset_all()
