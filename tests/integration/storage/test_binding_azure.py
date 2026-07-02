"""Integration tests for create_store_from_binding → Azure Blob Storage (real, keyless).

Verifies that the SDK builds a working Azure Blob store and authenticates
end-to-end by listing the container. Auth is KEYLESS Workload Identity
Federation: the integration storage account has shared-key access disabled, so
the binding carries the SP ``azureClientId`` + ``azureTenantId`` and a
``federatedTokenFile`` (the GitHub OIDC token) that obstore exchanges for an AAD
token — no account key or SAS. Embedded account-key / SAS resolution is covered
hermetically by the emulator tests (``test_emulator_azure.py``) and unit tests.

Prerequisites (set by the keyless CI job; or locally for an ad-hoc run):
    export AZURE_STORAGE_ACCOUNT=<account-name>
    export AZURE_STORAGE_CONTAINER=<existing-container>   # default: integ-test
    export AZURE_CLIENT_ID=<workload-identity-app-id>
    export AZURE_TENANT_ID=<tenant-id>
    export AZURE_FEDERATED_TOKEN_FILE=<path-to-oidc-token-file>

Run:
    uv run pytest tests/integration/storage/test_binding_azure.py -m azure_integration -v
"""

from __future__ import annotations

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
from tests.integration.storage.conftest import (
    AZURE_ACCOUNT,
    AZURE_CLIENT_ID,
    AZURE_CONTAINER,
    AZURE_FEDERATED_TOKEN_FILE,
    AZURE_TENANT_ID,
    write_dapr_component,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_auth(store) -> None:
    """Confirm auth works by listing the container root (no blobs required)."""
    async for _batch in obstore.list(store, prefix="integ-auth-probe/"):
        break


def _wi_metadata() -> dict[str, str]:
    """Workload-identity-federation binding metadata (no secret)."""
    return {
        "accountName": AZURE_ACCOUNT,
        "containerName": AZURE_CONTAINER,
        "azureClientId": AZURE_CLIENT_ID,
        "azureTenantId": AZURE_TENANT_ID,
        "federatedTokenFile": AZURE_FEDERATED_TOKEN_FILE,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.azure_integration
async def test_workload_identity_auth(tmp_path):
    """clientId + tenantId + federatedTokenFile (no secret) → list succeeds.

    The SDR / customer-infra production path on AKS: obstore exchanges the
    federated token for an AAD token using the SP, no account key required.
    """
    write_dapr_component(
        tmp_path / "components",
        name="azure-wi-store",
        binding_type="bindings.azure.blobstorage",
        metadata=_wi_metadata(),
    )
    store = create_store_from_binding(
        "azure-wi-store", components_dir=tmp_path / "components"
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
        metadata={**_wi_metadata(), "azureEnvironment": "AzureMarsCloud"},
    )
    with pytest.raises(StorageConfigError, match="Unknown azureEnvironment"):
        create_store_from_binding(
            "azure-env-store", components_dir=tmp_path / "components"
        )
