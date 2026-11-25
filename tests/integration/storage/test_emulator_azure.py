"""Hermetic integration test: ``create_store_from_binding`` → Azure Blob via Azurite.

Exercises the SDK's Azure binding wiring (``create_store_from_binding``) plus the
SDK's own storage abstractions — ``CloudStore`` + ``storage.ops`` — against a
local **Azurite** emulator. No real Azure, no direct ``obstore`` calls, and no
production-code change (``binding.py`` already maps ``endpoint`` + infers
``allow_http`` for the Azure branch).

Azurite ships a well-known dev account + key (public; valid only against the
emulator). Marked ``storage_emulator``. Local (10000 may be taken — any free
host port works, pass it via the env var):

    docker run -d --rm -p 10000:10000 \\
        mcr.microsoft.com/azure-storage/azurite:3.31.0 \\
        azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck
    # create container 'sdk-emulator-test' (az / azure-storage-blob)
    uv run pytest tests/integration/storage/test_emulator_azure.py -m storage_emulator -v
"""

from __future__ import annotations

import os

import pytest

from application_sdk.storage import ops
from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.cloud import CloudStore
from tests.integration.storage.conftest import (
    AZURITE_ACCOUNT,
    AZURITE_ENDPOINT,
    write_dapr_component,
)

pytestmark = [pytest.mark.storage_emulator, pytest.mark.usefixtures("require_azurite")]


# Well-known Azurite dev account key — public, emulator-only.
_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw=="
)
_BLOB_ENDPOINT = f"{AZURITE_ENDPOINT}/{AZURITE_ACCOUNT}"
_CONTAINER = os.environ.get("AZURE_EMULATOR_CONTAINER", "sdk-emulator-test")


def _azure_cloud_store(components_dir) -> CloudStore:
    """Build a CloudStore from an accountKey Azure Dapr component pointed at Azurite."""
    write_dapr_component(
        components_dir,
        name="azure-emulator",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURITE_ACCOUNT,
            "accountKey": _KEY,
            "containerName": _CONTAINER,
            "endpoint": _BLOB_ENDPOINT,
        },
    )
    store = create_store_from_binding("azure-emulator", components_dir=components_dir)
    return CloudStore(store, provider="azure")


async def test_azure_binding_roundtrip_via_sdk(tmp_path):
    """Azure binding round-trips through CloudStore (bytes) and ops (file) APIs."""
    cs = _azure_cloud_store(tmp_path / "components")

    key = "sdk-emulator/roundtrip.txt"
    payload = b"hello-from-sdk-azure-emulator-test"
    assert await cs.upload_bytes(key, payload) == len(payload)
    assert key in await cs.list(prefix="sdk-emulator/")
    assert await cs.get_bytes(key) == payload

    src = tmp_path / "ops-src.txt"
    src.write_bytes(payload)
    ops_key = "sdk-emulator/ops-roundtrip.txt"
    await ops.upload_file(ops_key, src, store=cs.store, normalize=False)
    assert await ops.exists(ops_key, store=cs.store)
    dl = tmp_path / "ops-dl.txt"
    await ops.download_file(ops_key, dl, store=cs.store, normalize=False)
    assert dl.read_bytes() == payload
    assert await ops.delete(ops_key, store=cs.store) is True
    assert not await ops.exists(ops_key, store=cs.store)

    await ops.delete(key, store=cs.store)
