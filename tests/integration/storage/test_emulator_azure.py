"""Hermetic integration test: ``create_store_from_binding`` → Azure Blob via Azurite.

Exercises the SDK's Azure binding wiring + obstore I/O against a local
**Azurite** emulator — no real Azure, and **no production-code changes**
(``binding.py`` already maps ``endpoint`` + infers ``allow_http`` for the Azure
branch). Shared SDK storage logic, tested once.

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

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
from tests.integration.storage.conftest import write_dapr_component

pytestmark = pytest.mark.storage_emulator

# Well-known Azurite dev account/key — public, emulator-only.
_ACCOUNT = "devstoreaccount1"
_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw=="
)
_ENDPOINT = os.environ.get("AZURE_STORAGE_ENDPOINT", "http://localhost:10000").rstrip(
    "/"
)
_BLOB_ENDPOINT = f"{_ENDPOINT}/{_ACCOUNT}"
_CONTAINER = os.environ.get("AZURE_EMULATOR_CONTAINER", "sdk-emulator-test")


@pytest.fixture(scope="module", autouse=True)
def require_azurite() -> None:
    """Skip the module when Azurite isn't reachable or unhealthy."""
    import httpx

    try:
        with httpx.Client(timeout=3.0) as client:
            # Azurite returns 400 (not 5xx) for the bare account URL — reachable.
            resp = client.get(_BLOB_ENDPOINT)
    except Exception as exc:  # pragma: no cover — env guard
        pytest.skip(f"Azurite not reachable at {_BLOB_ENDPOINT}: {exc}")
    if resp.status_code >= 500:  # pragma: no cover — env guard
        pytest.skip(f"Azurite unhealthy at {_BLOB_ENDPOINT}: HTTP {resp.status_code}")


async def test_azure_binding_roundtrip(tmp_path):
    """An accountKey Azure component pointed at Azurite round-trips put/list/get/delete."""
    write_dapr_component(
        tmp_path / "components",
        name="azure-emulator",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": _ACCOUNT,
            "accountKey": _KEY,
            "containerName": _CONTAINER,
            "endpoint": _BLOB_ENDPOINT,
        },
    )
    store = create_store_from_binding(
        "azure-emulator", components_dir=tmp_path / "components"
    )

    key = "sdk-emulator/roundtrip.txt"
    payload = b"hello-from-sdk-azure-emulator-test"
    await obstore.put_async(store, key, payload)

    listed = [
        obj["path"]
        async for batch in obstore.list(store, prefix="sdk-emulator/")
        for obj in batch
    ]
    assert key in listed

    result = await obstore.get_async(store, key)
    assert bytes(await result.bytes_async()) == payload

    await obstore.delete_async(store, key)
