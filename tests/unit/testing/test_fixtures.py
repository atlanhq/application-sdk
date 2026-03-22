"""Tests verifying pytest fixtures produce correctly wired instances."""

from __future__ import annotations

import pytest

from application_sdk.app.context import AppContext
from application_sdk.app.registry import AppRegistry, TaskRegistry
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

# Re-export fixtures so pytest discovers them
__all__ = [
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


def test_mock_state_store_fixture(mock_state_store: MockStateStore) -> None:
    assert isinstance(mock_state_store, MockStateStore)


def test_mock_secret_store_fixture(mock_secret_store: MockSecretStore) -> None:
    assert isinstance(mock_secret_store, MockSecretStore)


def test_mock_pubsub_fixture(mock_pubsub: MockPubSub) -> None:
    assert isinstance(mock_pubsub, MockPubSub)


def test_mock_binding_fixture(mock_binding: MockBinding) -> None:
    assert isinstance(mock_binding, MockBinding)


def test_mock_heartbeat_fixture(mock_heartbeat: MockHeartbeatController) -> None:
    assert isinstance(mock_heartbeat, MockHeartbeatController)


def test_mock_credential_store_fixture(
    mock_credential_store: MockCredentialStore,
) -> None:
    assert isinstance(mock_credential_store, MockCredentialStore)


def test_app_context_fixture_wired(
    app_context: AppContext,
    mock_state_store: MockStateStore,
    mock_secret_store: MockSecretStore,
) -> None:
    assert isinstance(app_context, AppContext)
    assert app_context._state_store is mock_state_store
    assert app_context._secret_store is mock_secret_store


@pytest.mark.asyncio
async def test_app_context_state_store_works(app_context: AppContext) -> None:
    await app_context.save_state("key", {"value": 42})
    result = await app_context.load_state("key")
    assert result == {"value": 42}


@pytest.mark.asyncio
async def test_app_context_secret_store_works(app_context: AppContext) -> None:
    assert app_context._secret_store is not None
    app_context._secret_store.set("pw", "secret")  # type: ignore[union-attr]
    result = await app_context.get_secret("pw")
    assert result == "secret"


def test_clean_app_registry_fixture(clean_app_registry: AppRegistry) -> None:
    assert isinstance(clean_app_registry, AppRegistry)
    assert clean_app_registry.list_apps() == []


def test_clean_app_registry_resets_after(clean_app_registry: AppRegistry) -> None:
    # Register something — the fixture should clean up after
    from application_sdk.contracts.base import Input, Output

    class _Input(Input):
        x: int = 0

    class _Output(Output):
        y: int = 0

    class _FakeApp:
        pass

    clean_app_registry.register("tmp-app", "1.0.0", _FakeApp, _Input, _Output)
    assert "tmp-app" in clean_app_registry.list_apps()
    # After the test, the fixture teardown calls AppRegistry.reset()


def test_clean_task_registry_fixture(clean_task_registry: TaskRegistry) -> None:
    assert isinstance(clean_task_registry, TaskRegistry)
    assert clean_task_registry.list_apps_with_tasks() == []
