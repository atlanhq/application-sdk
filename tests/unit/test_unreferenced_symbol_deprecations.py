"""Public names with no caller in the SDK warn before their v4.0.0 removal.

Each one is still importable and still works; calling it emits a
``DeprecationWarning`` that names the replacement and the removal version.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.app.context import AppContext, ChildAppContext
from application_sdk.app.registry import AppNotRegisteredError, AppRegistry
from application_sdk.clients.azure.azure_errors import (
    AzureConnectionError,
    AzureServiceError,
)
from application_sdk.infrastructure._dapr.client import (
    create_dapr_secret_store,
    is_dapr_component_registered,
)
from application_sdk.infrastructure._dapr.credential_vault import (
    create_dapr_credential_vault,
)
from application_sdk.observability.cgroup import memory_fraction
from application_sdk.observability.resource_sampler import compute_deltas
from application_sdk.observability.trace_context import get_trace_context


def _child_app_context() -> object:
    parent = AppContext(app_name="p", app_version="1", run_id="r")
    return ChildAppContext(parent=parent, child_app_name="kid")


def _get_all_versions() -> object:
    with pytest.raises(AppNotRegisteredError):
        AppRegistry().get_all_versions("no-such-app")
    return None


def _create_dapr_credential_vault() -> object:
    with patch(
        "application_sdk.infrastructure._dapr.credential_vault.DaprCredentialVault"
    ):
        return create_dapr_credential_vault(MagicMock())


_SYNC_CALLS: dict[str, Callable[[], Any]] = {
    "get_trace_context": get_trace_context,
    "compute_deltas": lambda: compute_deltas(None, None, 1.0),
    "memory_fraction": memory_fraction,
    "ChildAppContext": _child_app_context,
    "AzureConnectionError": AzureConnectionError,
    "AzureServiceError": AzureServiceError,
    "get_all_versions": _get_all_versions,
    "create_dapr_secret_store": lambda: create_dapr_secret_store(MagicMock()),
    "create_dapr_credential_vault": _create_dapr_credential_vault,
}


@pytest.mark.parametrize("name", sorted(_SYNC_CALLS))
def test_unreferenced_public_name_warns(name: str) -> None:
    with pytest.warns(DeprecationWarning, match=r"v4\.0\.0") as record:
        _SYNC_CALLS[name]()
    assert any(name in str(w.message) for w in record)


async def test_is_dapr_component_registered_warns() -> None:
    client = AsyncMock()
    client.get_metadata.return_value = {"components": []}
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch(
            "application_sdk.infrastructure._dapr.client.AsyncDaprClient",
            return_value=client,
        ),
        pytest.warns(DeprecationWarning, match="is_dapr_component_registered"),
    ):
        assert await is_dapr_component_registered("statestore") is False
