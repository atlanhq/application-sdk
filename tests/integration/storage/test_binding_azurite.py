"""Integration tests for create_store_from_binding → Azurite (Azure Blob emulator).

Prerequisites:
    docker compose -f tests/integration/storage/docker-compose.yml up -d azurite

Run:
    uv run pytest tests/integration/storage/test_binding_azurite.py -m azure_integration -v

Requires the ``azure`` extra for container setup and SAS generation:
    uv sync --extra azure

All tests use ``create_store_from_binding`` against a real Dapr component YAML —
no monkey-patching of the SDK factory.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
from tests.integration.storage.conftest import (
    AZURITE_ACCOUNT,
    AZURITE_BLOB_ENDPOINT,
    AZURITE_CONTAINER,
    AZURITE_KEY,
    write_dapr_component,
)

# ---------------------------------------------------------------------------
# Module-level setup: ensure the test container exists in Azurite.
# Requires azure-storage-blob (azure extra).
# ---------------------------------------------------------------------------

azure_storage_blob = pytest.importorskip(
    "azure.storage.blob",
    reason="azure extra required for Azurite tests: uv sync --extra azure",
)
BlobServiceClient = azure_storage_blob.BlobServiceClient
generate_container_sas = azure_storage_blob.generate_container_sas
ContainerSasPermissions = azure_storage_blob.ContainerSasPermissions


def _ensure_container() -> None:
    """Create the Azurite test container if it does not already exist."""
    client = BlobServiceClient(
        account_url=AZURITE_BLOB_ENDPOINT, credential=AZURITE_KEY
    )
    with suppress(Exception):
        client.create_container(AZURITE_CONTAINER)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_prefix() -> str:
    return f"integ/{uuid.uuid4().hex[:8]}"


async def _round_trip(store, prefix: str, content: bytes = b"hello-azurite") -> None:
    """Put *content* at ``{prefix}/data.bin``, get it back, assert equality."""
    key = f"{prefix}/data.bin"
    await obstore.put_async(store, key, content)
    result = await obstore.get_async(store, key)
    assert bytes(result.bytes()) == content

    keys = []
    async for batch in obstore.list(store, prefix=prefix):
        for item in batch:
            keys.append(str(item["path"]))
    assert key in keys


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.azure_integration
async def test_account_key_round_trip(tmp_path):
    """accountKey + containerName + accountName → put/get/list round-trip."""
    _ensure_container()
    write_dapr_component(
        tmp_path / "components",
        name="azurite-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURITE_ACCOUNT,
            "containerName": AZURITE_CONTAINER,
            "accountKey": AZURITE_KEY,
            "endpoint": AZURITE_BLOB_ENDPOINT,
        },
    )
    store = create_store_from_binding(
        "azurite-store", components_dir=tmp_path / "components"
    )
    await _round_trip(store, _unique_prefix())


@pytest.mark.azure_integration
async def test_use_emulator_flag(tmp_path):
    """useEmulator=true routes traffic to the Azurite blob endpoint."""
    _ensure_container()
    write_dapr_component(
        tmp_path / "components",
        name="azurite-emulator-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURITE_ACCOUNT,
            "containerName": AZURITE_CONTAINER,
            "accountKey": AZURITE_KEY,
            "useEmulator": "true",
        },
    )
    store = create_store_from_binding(
        "azurite-emulator-store", components_dir=tmp_path / "components"
    )
    await _round_trip(store, _unique_prefix(), content=b"emulator-flag-test")


@pytest.mark.azure_integration
async def test_sas_token_round_trip(tmp_path):
    """sasToken auth → put/get/list round-trip via Azurite SAS support."""
    _ensure_container()

    sas_token = generate_container_sas(
        account_name=AZURITE_ACCOUNT,
        container_name=AZURITE_CONTAINER,
        account_key=AZURITE_KEY,
        permission=ContainerSasPermissions(
            read=True, write=True, list=True, delete=True
        ),
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )

    write_dapr_component(
        tmp_path / "components",
        name="azurite-sas-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURITE_ACCOUNT,
            "containerName": AZURITE_CONTAINER,
            "sasToken": sas_token,
            "endpoint": AZURITE_BLOB_ENDPOINT,
        },
    )
    store = create_store_from_binding(
        "azurite-sas-store", components_dir=tmp_path / "components"
    )
    await _round_trip(store, _unique_prefix(), content=b"sas-token-test")


@pytest.mark.azure_integration
async def test_sas_key_alias(tmp_path):
    """``sasKey`` is accepted as an alias for ``sasToken``."""
    _ensure_container()

    sas_token = generate_container_sas(
        account_name=AZURITE_ACCOUNT,
        container_name=AZURITE_CONTAINER,
        account_key=AZURITE_KEY,
        permission=ContainerSasPermissions(
            read=True, write=True, list=True, delete=True
        ),
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )

    write_dapr_component(
        tmp_path / "components",
        name="azurite-saskey-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURITE_ACCOUNT,
            "containerName": AZURITE_CONTAINER,
            "sasKey": sas_token,
            "endpoint": AZURITE_BLOB_ENDPOINT,
        },
    )
    store = create_store_from_binding(
        "azurite-saskey-store", components_dir=tmp_path / "components"
    )
    await _round_trip(store, _unique_prefix(), content=b"sas-key-alias-test")


@pytest.mark.azure_integration
async def test_custom_endpoint_field(tmp_path):
    """endpoint field routes traffic to a custom host (Azurite)."""
    _ensure_container()
    write_dapr_component(
        tmp_path / "components",
        name="azurite-endpoint-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURITE_ACCOUNT,
            "containerName": AZURITE_CONTAINER,
            "accountKey": AZURITE_KEY,
            "endpoint": AZURITE_BLOB_ENDPOINT,
        },
    )
    store = create_store_from_binding(
        "azurite-endpoint-store", components_dir=tmp_path / "components"
    )
    await _round_trip(store, _unique_prefix(), content=b"custom-endpoint-test")


@pytest.mark.azure_integration
async def test_multipart_upload(tmp_path):
    """Upload a >5 MiB object to exercise the multipart code path against Azurite."""
    _ensure_container()
    write_dapr_component(
        tmp_path / "components",
        name="azurite-mp-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURITE_ACCOUNT,
            "containerName": AZURITE_CONTAINER,
            "accountKey": AZURITE_KEY,
            "endpoint": AZURITE_BLOB_ENDPOINT,
        },
    )
    store = create_store_from_binding(
        "azurite-mp-store", components_dir=tmp_path / "components"
    )
    payload = b"z" * (6 * 1024 * 1024)
    await _round_trip(store, _unique_prefix(), content=payload)


@pytest.mark.azure_integration
async def test_unknown_azure_environment_raises(tmp_path):
    """Unknown azureEnvironment value raises StorageConfigError."""
    from application_sdk.storage.errors import StorageConfigError

    write_dapr_component(
        tmp_path / "components",
        name="azurite-env-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURITE_ACCOUNT,
            "containerName": AZURITE_CONTAINER,
            "accountKey": AZURITE_KEY,
            "azureEnvironment": "AzureMarsCloud",
        },
    )
    with pytest.raises(StorageConfigError, match="Unknown azureEnvironment"):
        create_store_from_binding(
            "azurite-env-store", components_dir=tmp_path / "components"
        )
