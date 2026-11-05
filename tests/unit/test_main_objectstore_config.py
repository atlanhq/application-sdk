"""Startup diagnostics for the required deployment object store."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk import main
from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME
from application_sdk.storage.errors import (
    StorageBindingNotFoundError,
    StorageConfigError,
)


@pytest.mark.parametrize("registered", [False, True])
async def test_disabled_objectstore_logs_required_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", "false")
    client = Mock()
    names = [DEPLOYMENT_OBJECT_STORE_NAME] if registered else []
    client.get_metadata = AsyncMock(
        return_value={"components": [{"name": name} for name in names]}
    )

    with patch.object(main, "logger") as log:
        result = await main._log_dapr_components(client, tmp_path)

    assert result == set(names)
    messages = [call.args[0] % call.args[1:] for call in log.error.call_args_list]
    assert any(
        "role=object_store" in message
        and "requires the deployment object store" in message
        and "deploy.dapr.objectstore" in message
        for message in messages
    )
    info_messages = [call.args[0] % call.args[1:] for call in log.info.call_args_list]
    assert not any(
        "role=object_store" in message and "(expected)" in message
        for message in info_messages
    )


@pytest.mark.parametrize("metadata_unavailable", [False, True])
async def test_missing_disabled_objectstore_raises_actionable_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata_unavailable: bool
) -> None:
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    monkeypatch.setenv("DAPR_COMPONENTS_PATH", str(tmp_path))
    monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", "false")
    client = Mock()
    client.get_metadata = AsyncMock(return_value={"components": []})
    if metadata_unavailable:
        client.get_metadata.side_effect = RuntimeError("metadata unavailable")

    with (
        patch(
            "application_sdk.infrastructure._dapr.http.wait_for_dapr_sidecar",
            new=AsyncMock(),
        ),
        patch(
            "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
            return_value=client,
        ),
        pytest.raises(StorageConfigError, match=r"deploy\.dapr\.objectstore") as exc,
    ):
        await main._create_infrastructure()

    assert type(exc.value) is StorageConfigError
    assert exc.value.effective_retryable is False
    assert isinstance(exc.value.__cause__, StorageBindingNotFoundError)


async def test_disabled_objectstore_with_mounted_component_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    monkeypatch.setenv("DAPR_COMPONENTS_PATH", str(tmp_path))
    monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", "false")
    client = Mock()
    client.get_metadata = AsyncMock(
        return_value={"components": [{"name": DEPLOYMENT_OBJECT_STORE_NAME}]}
    )
    store = Mock()
    with (
        patch(
            "application_sdk.infrastructure._dapr.http.wait_for_dapr_sidecar",
            new=AsyncMock(),
        ),
        patch(
            "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
            return_value=client,
        ),
        patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", False),
        patch(
            "application_sdk.storage.create_store_from_binding_with_put_attrs",
            return_value=(store, None),
        ) as resolver,
        patch(
            "application_sdk.storage.binding._create_store_from_binding_optional_with_put_attrs",
            return_value=(None, None),
        ),
    ):
        infra = await main._create_infrastructure()

    assert infra.storage is store
    resolver.assert_called_once_with(
        DEPLOYMENT_OBJECT_STORE_NAME, components_dir=tmp_path, secrets={}
    )


@pytest.mark.parametrize("enabled", [None, "true"])
async def test_missing_objectstore_preserves_not_found_without_disabled_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: str | None
) -> None:
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    monkeypatch.setenv("DAPR_COMPONENTS_PATH", str(tmp_path))
    if enabled is None:
        monkeypatch.delenv("ATLAN_ENABLE_OBJECTSTORE", raising=False)
    else:
        monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", enabled)
    client = Mock()
    client.get_metadata = AsyncMock(return_value={"components": []})

    with (
        patch(
            "application_sdk.infrastructure._dapr.http.wait_for_dapr_sidecar",
            new=AsyncMock(),
        ),
        patch(
            "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
            return_value=client,
        ),
        pytest.raises(StorageBindingNotFoundError),
    ):
        await main._create_infrastructure()
