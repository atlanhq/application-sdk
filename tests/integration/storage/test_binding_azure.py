"""Integration tests for create_store_from_binding → Azure Blob Storage (real service).

Verifies that each supported auth mode is correctly wired end-to-end by calling
``list`` on the resulting store. No data is written or deleted.

Prerequisites:
    export AZURE_STORAGE_ACCOUNT=<account-name>
    export AZURE_STORAGE_KEY=<account-key>
    export AZURE_STORAGE_CONTAINER=<existing-container>   # default: integ-test

Run:
    uv run pytest tests/integration/storage/test_binding_azure.py -m azure_integration -v

Requires the ``azure`` extra for SAS generation:
    uv sync --extra azure

All tests use ``create_store_from_binding`` against a real Dapr component YAML —
no monkey-patching of the SDK factory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
from tests.integration.storage.conftest import (
    AZURE_ACCOUNT,
    AZURE_CONTAINER,
    AZURE_KEY,
    write_dapr_component,
)

# ---------------------------------------------------------------------------
# Optional SAS helpers (azure extra)
# ---------------------------------------------------------------------------

azure_storage_blob = pytest.importorskip(
    "azure.storage.blob",
    reason="azure extra required for SAS tests: uv sync --extra azure",
)
generate_container_sas = azure_storage_blob.generate_container_sas
ContainerSasPermissions = azure_storage_blob.ContainerSasPermissions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_auth(store) -> None:
    """Confirm auth works by listing the container root (no blobs required)."""
    async for _batch in obstore.list(store, prefix="integ-auth-probe/"):
        break


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.azure_integration
async def test_account_key_auth(tmp_path):
    """accountKey + containerName + accountName → list succeeds (Shared Key auth)."""
    write_dapr_component(
        tmp_path / "components",
        name="azure-key-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURE_ACCOUNT,
            "containerName": AZURE_CONTAINER,
            "accountKey": AZURE_KEY,
        },
    )
    store = create_store_from_binding(
        "azure-key-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)


@pytest.mark.azure_integration
async def test_sas_token_auth(tmp_path):
    """sasToken auth → list succeeds."""
    sas_token = generate_container_sas(
        account_name=AZURE_ACCOUNT,
        container_name=AZURE_CONTAINER,
        account_key=AZURE_KEY,
        permission=ContainerSasPermissions(read=True, list=True),
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    write_dapr_component(
        tmp_path / "components",
        name="azure-sas-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURE_ACCOUNT,
            "containerName": AZURE_CONTAINER,
            "sasToken": sas_token,
        },
    )
    store = create_store_from_binding(
        "azure-sas-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)


@pytest.mark.azure_integration
async def test_sas_key_alias(tmp_path):
    """``sasKey`` is accepted as an alias for ``sasToken`` → list succeeds."""
    sas_token = generate_container_sas(
        account_name=AZURE_ACCOUNT,
        container_name=AZURE_CONTAINER,
        account_key=AZURE_KEY,
        permission=ContainerSasPermissions(read=True, list=True),
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    write_dapr_component(
        tmp_path / "components",
        name="azure-saskey-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURE_ACCOUNT,
            "containerName": AZURE_CONTAINER,
            "sasKey": sas_token,
        },
    )
    store = create_store_from_binding(
        "azure-saskey-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)


@pytest.mark.azure_integration
async def test_unknown_azure_environment_raises(tmp_path):
    """Unknown azureEnvironment value raises StorageConfigError at store creation."""
    from application_sdk.storage.errors import StorageConfigError

    write_dapr_component(
        tmp_path / "components",
        name="azure-env-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURE_ACCOUNT,
            "containerName": AZURE_CONTAINER,
            "accountKey": AZURE_KEY,
            "azureEnvironment": "AzureMarsCloud",
        },
    )
    with pytest.raises(StorageConfigError, match="Unknown azureEnvironment"):
        create_store_from_binding(
            "azure-env-store", components_dir=tmp_path / "components"
        )
