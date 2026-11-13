"""Hermetic integration test: the API host's config and file routes on obstore.

The consolidated host builds its store with :class:`HostObjectStore` over the
same constructors the worker uses (``application_sdk._obstore_config``). These
drive the host's routes against a real S3 protocol (**MinIO**) and a real Azure
Blob protocol (**Azurite**), with the emulator's endpoint in place of the
cloud's, and pin the status codes the routes promise:

* a config round-trips under the hosted app's key;
* a missing config is 404;
* a missing bucket or container is 503, although S3 and Azure answer it with
  a 404 too;
* an upload lands under the app's namespace with its bytes.

Marked ``storage_emulator``; see ``test_emulator_s3.py`` and
``test_emulator_azure.py`` for running the emulators locally.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import obstore
import pytest
from fastapi.testclient import TestClient

from application_sdk._obstore_config import (
    make_azure_store,
    make_s3_store,
    obstore_client_options,
)
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    MetadataInput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)
from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.object_routes import HostObjectStore
from tests.integration.storage.conftest import emulator_unavailable

pytestmark = pytest.mark.storage_emulator

_S3_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:9000")
_S3_USER = os.environ.get("MINIO_ROOT_USER", "minioadmin")
_S3_PASS = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
_S3_BUCKET = os.environ.get("S3_EMULATOR_BUCKET", "sdk-emulator-test")

# Well-known Azurite dev account/key — public, emulator-only.
_AZ_ACCOUNT = "devstoreaccount1"
_AZ_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw=="
)
_AZ_ENDPOINT = os.environ.get(
    "AZURE_STORAGE_ENDPOINT", "http://localhost:10000"
).rstrip("/")
_AZ_CONTAINER = os.environ.get("AZURE_EMULATOR_CONTAINER", "sdk-emulator-test")


class _Handler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[])


def _reachable(url: str, name: str) -> None:
    import httpx

    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(url)
    except Exception as exc:  # pragma: no cover — env guard
        emulator_unavailable(f"{name} not reachable at {url}: {exc}")
    if resp.status_code >= 500:  # pragma: no cover — env guard
        emulator_unavailable(f"{name} unhealthy at {url}: HTTP {resp.status_code}")


def _http_options() -> dict[str, object]:
    return {**obstore_client_options(), "allow_http": True}


def _minio(bucket: str) -> HostObjectStore:
    _reachable(f"{_S3_ENDPOINT}/minio/health/live", "MinIO")
    store = make_s3_store(
        bucket,
        {
            "aws_endpoint": _S3_ENDPOINT,
            "aws_region": "us-east-1",
            "aws_access_key_id": _S3_USER,
            "aws_secret_access_key": _S3_PASS,
            "aws_virtual_hosted_style_request": "false",
            "aws_disable_tagging": "true",
        },
        label="host-s3",
        client_options=_http_options(),  # type: ignore[arg-type]
    )
    return HostObjectStore(store, kind="s3")


def _azurite(container: str) -> HostObjectStore:
    _reachable(f"{_AZ_ENDPOINT}/{_AZ_ACCOUNT}", "Azurite")
    store = make_azure_store(
        container,
        {
            "azure_storage_account_name": _AZ_ACCOUNT,
            "azure_storage_account_key": _AZ_KEY,
            "azure_storage_endpoint": f"{_AZ_ENDPOINT}/{_AZ_ACCOUNT}",
        },
        label="host-azure",
        client_options=_http_options(),  # type: ignore[arg-type]
    )
    return HostObjectStore(store, kind="azure")


_STORES: dict[str, tuple[Callable[[str], HostObjectStore], str]] = {
    "s3": (_minio, _S3_BUCKET),
    "azure": (_azurite, _AZ_CONTAINER),
}


def _host(store: HostObjectStore) -> TestClient:
    return TestClient(
        build_asgi_app(_Handler(), app_name="mysql", object_store=store),
        raise_server_exceptions=False,
    )


@pytest.mark.parametrize("cloud", sorted(_STORES))
def test_a_config_round_trips_and_a_missing_one_is_404(cloud: str) -> None:
    make, existing = _STORES[cloud]
    store = make(existing)
    host = _host(store)
    body = {"host": "db.example", "port": 3306}
    config_id = f"host-emulator-{cloud}"
    key = f"persistent-artifacts/apps/mysql/credentials/{config_id}/config.json"
    try:
        saved = host.post(
            f"/workflows/v1/config/{config_id}?type=credentials", json=body
        )
        assert saved.status_code == 200
        fetched = host.get(f"/workflows/v1/config/{config_id}?type=credentials")
        assert fetched.status_code == 200
        assert fetched.json()["data"] == body
        missing = host.get("/workflows/v1/config/host-emulator-absent?type=credentials")
        assert missing.status_code == 404
    finally:
        obstore.delete(store._store, key)


@pytest.mark.parametrize("cloud", sorted(_STORES))
def test_a_missing_bucket_or_container_is_503_not_404(cloud: str) -> None:
    make, _ = _STORES[cloud]
    host = _host(make("host-emulator-no-such-bucket"))
    response = host.get("/workflows/v1/config/g1?type=credentials")
    assert response.status_code == 503
    assert response.json()["detail"] == "Object store unavailable"


@pytest.mark.parametrize("cloud", sorted(_STORES))
def test_an_upload_lands_under_the_apps_namespace(cloud: str) -> None:
    make, existing = _STORES[cloud]
    store = make(existing)
    response = _host(store).post(
        "/workflows/v1/file",
        files={"file": ("rows.csv", b"a,b\n1,2\n", "text/csv")},
        data={"prefix": "sample_data"},
    )
    assert response.status_code == 200
    key = response.json()["data"]["key"]
    try:
        assert key.startswith("persistent-artifacts/apps/mysql/uploads/sample_data/")
        assert bytes(obstore.get(store._store, key).bytes()) == b"a,b\n1,2\n"
    finally:
        obstore.delete(store._store, key)
