"""The host's object store on a native Azure Blob tenant, and per-app stores.

On those tenants the chart gives every app's Dapr object store a
``bindings.azure.blobstorage`` component (account, account key, container) and
no S3 bucket, so the host reaches the same container over the Blob REST API.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from application_sdk.handler import object_routes
from application_sdk.handler.asgi import (
    app_object_store,
    build_asgi_app,
    default_object_store,
)
from application_sdk.handler.object_routes import (
    AzureBlobObjectStore,
    S3ObjectStore,
    object_store_from_env,
)

from .test_object_routes_host import _AZURE_BLOB_HOST_ENV, _Handler, _Log

#: A synthetic account key, base64 as Azure issues them.
_KEY = base64.b64encode(b"synthetic-test-account-key-0123456789").decode()
_DATE = "Thu, 12 Nov 2026 07:00:00 GMT"

#: Signatures azure-storage-blob 12.30.1's SharedKeyCredentialPolicy computes for
#: the same requests (account ``exampleaccount``, key ``_KEY``, ``_DATE``).
_GET_SIG = "SharedKey exampleaccount:7XWn0gG1voRYNwuPCmlZZdBz5xdpkP69Qnp65BJnP4E="
_PUT_SIG = "SharedKey exampleaccount:obJHQxFDQODDZgxwKmi1VH6KZgEbQoV4jkA+nJXmVa8="
_EMPTY_PUT_SIG = "SharedKey exampleaccount:B0CEysM6Nt0cTGhe3e+j1t4TBDRhOBIPe6LFUquupEY="


class _Blobs:
    """A Blob service over a dict, answering the store's requests in-process."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.missing_container = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.missing_container:
            return httpx.Response(404, headers={"x-ms-error-code": "ContainerNotFound"})
        path = request.url.path
        if request.method == "PUT":
            self.blobs[path] = request.read()
            return httpx.Response(201)
        if path not in self.blobs:
            return httpx.Response(404, headers={"x-ms-error-code": "BlobNotFound"})
        return httpx.Response(200, content=self.blobs[path])


@pytest.fixture
def blobs(monkeypatch: pytest.MonkeyPatch) -> _Blobs:
    monkeypatch.setattr(object_routes, "formatdate", lambda usegmt: _DATE)
    return _Blobs()


def _store(blobs: _Blobs) -> AzureBlobObjectStore:
    return AzureBlobObjectStore(
        "exampleaccount",
        "objectstore",
        _KEY,
        client=httpx.Client(transport=httpx.MockTransport(blobs)),
    )


async def test_requests_are_signed_as_azure_storage_blob_signs_them(
    blobs: _Blobs,
) -> None:
    store = _store(blobs)
    await store.get_json("wfhealth/default/snapshot.json")
    await store.put_json(
        "persistent-artifacts/apps/my app/credentials/g1/config.json",
        {"host": "db.xyz"},  # 17 bytes, as signed
    )

    get, put = blobs.requests
    assert get.url.host == "exampleaccount.blob.core.windows.net"
    assert get.headers["Authorization"] == _GET_SIG
    # The path sent is the path signed.
    assert (
        put.url.raw_path
        == b"/objectstore/persistent-artifacts/apps/my%20app/credentials/g1/config.json"
    )
    assert put.headers["Content-Length"] == "17"
    assert put.headers["x-ms-blob-type"] == "BlockBlob"
    assert put.headers["Authorization"] == _PUT_SIG


async def test_an_empty_upload_signs_an_empty_content_length(
    blobs: _Blobs, tmp_path: Path
) -> None:
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    await _store(blobs).put_file("empty.json", str(empty))
    assert blobs.requests[0].headers["Authorization"] == _EMPTY_PUT_SIG


@pytest.fixture
def host(blobs: _Blobs) -> TestClient:
    return TestClient(
        build_asgi_app(_Handler(), app_name="mysql", object_store=_store(blobs)),
        raise_server_exceptions=False,
    )


def test_a_config_round_trips_through_the_container(
    host: TestClient, blobs: _Blobs
) -> None:
    body = {"host": "db.example", "port": 3306}
    assert (
        host.post("/workflows/v1/config/g1?type=credentials", json=body).status_code
        == 200
    )
    key = "/objectstore/persistent-artifacts/apps/mysql/credentials/g1/config.json"
    assert key in blobs.blobs
    fetched = host.get("/workflows/v1/config/g1?type=credentials")
    assert fetched.status_code == 200 and fetched.json()["data"] == body


def test_a_missing_blob_is_404_and_a_missing_container_is_503(
    host: TestClient, blobs: _Blobs
) -> None:
    assert host.get("/workflows/v1/config/nope?type=credentials").status_code == 404
    blobs.missing_container = True
    assert host.get("/workflows/v1/config/nope?type=credentials").status_code == 503


def test_a_file_upload_is_stored_under_its_key(host: TestClient, blobs: _Blobs) -> None:
    response = host.post(
        "/workflows/v1/file",
        files={"file": ("rows.csv", b"a,b\n1,2\n", "text/csv")},
        data={"prefix": "sample_data"},
    )
    assert response.status_code == 200
    key = response.json()["data"]["key"]
    assert key.startswith("persistent-artifacts/apps/mysql/uploads/sample_data/")
    assert blobs.blobs[f"/objectstore/{key}"] == b"a,b\n1,2\n"


#: The host's env on a native Azure Blob tenant once its manifest aliases the
#: chart's ``AZURE_STORAGE_*`` as ``ATLAN_AZURE_STORAGE_*``.
_AZURE_ALIASES = {
    "ATLAN_AZURE_STORAGE_ACCOUNT": "exampleaccount",
    "ATLAN_AZURE_STORAGE_ACCESS_KEY": _KEY,
    "ATLAN_AZURE_STORAGE_CONTAINER": "objectstore",
}


def _env(values: dict[str, str]):
    return lambda name: values.get(name, "")


def test_on_azure_blob_the_store_is_the_apps_container() -> None:
    store = object_store_from_env(_env(_AZURE_ALIASES))
    assert isinstance(store, AzureBlobObjectStore)
    assert store._container == "objectstore"
    assert _KEY not in repr(store)


def test_a_bucket_wins_over_an_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Azure tenant with S3 enabled gets an S3 Dapr store: so does the host."""
    monkeypatch.setattr(
        S3ObjectStore, "__init__", lambda self, bucket, region=None, client=None: None
    )
    env = {**_AZURE_ALIASES, "ATLAN_S3_BUCKET": "tenant-bucket"}
    assert isinstance(object_store_from_env(_env(env)), S3ObjectStore)


@pytest.mark.parametrize(
    ("missing", "value"),
    [
        ("ATLAN_AZURE_STORAGE_ACCESS_KEY", ""),
        ("ATLAN_AZURE_STORAGE_CONTAINER", ""),
        ("ATLAN_AZURE_STORAGE_ACCESS_KEY", "not base64!"),
    ],
)
def test_an_incomplete_azure_store_is_none_and_logs_no_key(
    monkeypatch: pytest.MonkeyPatch, missing: str, value: str
) -> None:
    log = _Log()
    monkeypatch.setattr(object_routes, "logger", log)
    env = {**_AZURE_ALIASES, missing: value}
    assert object_store_from_env(_env(env)) is None
    assert len(log.lines) == 1 and missing in log.lines[0]
    assert not any(_KEY in line or "not base64" in line for line in log.lines)


@pytest.fixture
def fresh_stores() -> Iterator[None]:
    """Both store builders are cached per process: read the env afresh."""
    default_object_store.cache_clear()
    app_object_store.cache_clear()
    yield
    default_object_store.cache_clear()
    app_object_store.cache_clear()


def _set(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name in (
        "ATLAN_S3_BUCKET",
        "ATLAN_GCP_BUCKET",
        *_AZURE_ALIASES,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("fresh_stores")
def test_the_hosts_default_store_reads_its_azure_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, {**_AZURE_BLOB_HOST_ENV, **_AZURE_ALIASES})
    assert isinstance(default_object_store(), AzureBlobObjectStore)


@pytest.mark.usefixtures("fresh_stores")
def test_unresolved_azure_aliases_are_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """On AWS or GCP the chart injects no ``AZURE_STORAGE_*``."""
    _set(
        monkeypatch,
        {name: f"$({name.removeprefix('ATLAN_')})" for name in _AZURE_ALIASES},
    )
    assert default_object_store() is None


_SW = "ATLAN_APPENV__SYSTEM_WORKFLOWS__"


@pytest.mark.usefixtures("fresh_stores")
def test_an_app_that_declares_its_store_gets_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its own container and key, not the host's, even when the host has one."""
    _set(monkeypatch, {"ATLAN_S3_BUCKET": "host-bucket", "ATLAN_S3_REGION": "x"})
    for name, value in _AZURE_ALIASES.items():
        monkeypatch.setenv(_SW + name, value)
    monkeypatch.setenv(_SW + "ATLAN_AZURE_STORAGE_CONTAINER", "sw-container")
    # Its unresolved S3 alias (it declares every cloud's) is not a bucket.
    monkeypatch.setenv(_SW + "ATLAN_S3_BUCKET", "$(S3_BUCKET)")

    store = app_object_store("system-workflows")

    assert isinstance(store, AzureBlobObjectStore)
    assert store._container == "sw-container"
    assert app_object_store("system-workflows") is store


@pytest.mark.usefixtures("fresh_stores")
def test_an_app_with_a_partial_store_never_falls_back_to_the_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        S3ObjectStore, "__init__", lambda self, bucket, region=None, client=None: None
    )
    _set(monkeypatch, {"ATLAN_S3_BUCKET": "host-bucket"})
    monkeypatch.setenv(_SW + "ATLAN_AZURE_STORAGE_ACCOUNT", "exampleaccount")
    assert app_object_store("system-workflows") is None


@pytest.mark.usefixtures("fresh_stores")
def test_an_app_without_its_own_store_gets_the_hosts_unless_own_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(monkeypatch, _AZURE_ALIASES)
    assert app_object_store("mysql") is default_object_store()
    assert app_object_store("mysql") is not None
    assert app_object_store("mysql", own_only=True) is None


@pytest.mark.usefixtures("fresh_stores")
def test_the_routes_use_the_apps_own_store(
    monkeypatch: pytest.MonkeyPatch, blobs: _Blobs
) -> None:
    init = AzureBlobObjectStore.__init__

    def _in_process(
        self: AzureBlobObjectStore, *args: object, **kwargs: object
    ) -> None:
        init(self, *args, client=httpx.Client(transport=httpx.MockTransport(blobs)))  # type: ignore[arg-type]

    monkeypatch.setattr(AzureBlobObjectStore, "__init__", _in_process)
    _set(monkeypatch, {})
    for name, value in _AZURE_ALIASES.items():
        monkeypatch.setenv(_SW + name, value)
    sw = TestClient(build_asgi_app(_Handler(), app_name="system-workflows"))
    other = TestClient(build_asgi_app(_Handler(), app_name="mysql"))

    assert (
        sw.post("/workflows/v1/config/g1?type=credentials", json={}).status_code == 200
    )
    assert list(blobs.blobs) == [
        "/objectstore/persistent-artifacts/apps/system-workflows/credentials/g1/config.json"
    ]
    assert (
        other.post("/workflows/v1/config/g1?type=credentials", json={}).status_code
        == 503
    )
