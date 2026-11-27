"""The config and file routes on the consolidated API host.

Heracles writes a credential's non-secret fields to the app's config on every
credential create and update and before every run submit, and the app's worker
reads them back at run time. On the host the write must land on exactly the
key the worker reads, under the hosted app's name — never the host's own.

The host's store is :class:`HostObjectStore` over obstore, built from the
chart-injected env with the worker's own constructors.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import obstore
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from obstore.store import LocalStore, MemoryStore

from application_sdk import _obstore_config
from application_sdk.handler import (
    object_routes,
)
from application_sdk.handler.asgi import build_asgi_app, default_object_store
from application_sdk.handler.object_routes import HostObjectStore
from tests.unit._doubles import StaticHandler


def _stored(store: MemoryStore) -> dict[str, bytes]:
    """Every object in *store*, by key."""
    keys = [meta["path"] for batch in obstore.list(store) for meta in batch]
    return {key: bytes(obstore.get(store, key).bytes()) for key in keys}


@pytest.fixture(autouse=True)
def _sidecars_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sidecar emission at its default, whatever the runner's env says.

    ``constants`` is loaded in this process, as on the worker, so its value is
    the one ``storage._settings`` reads; on the host the env is read.
    """
    monkeypatch.setenv("ATLAN_STORAGE_WRITE_SIDECARS", "true")
    monkeypatch.setattr("application_sdk.constants.STORAGE_WRITE_SIDECARS", True)


@pytest.fixture
def memory() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def host(memory: MemoryStore) -> TestClient:
    store = HostObjectStore(memory, kind="memory")
    return TestClient(
        build_asgi_app(StaticHandler(), app_name="mysql", object_store=store),
        raise_server_exceptions=False,
    )


# --- the routes over obstore ------------------------------------------------


def test_a_credential_config_lands_where_the_apps_worker_reads_it(
    host: TestClient, memory: MemoryStore
) -> None:
    body = {"host": "db.example", "port": 3306, "authType": "basic"}
    response = host.post("/workflows/v1/config/guid-1?type=credentials", json=body)
    assert response.status_code == 200
    key = "persistent-artifacts/apps/mysql/credentials/guid-1/config.json"
    assert key in _stored(memory)
    assert obstore.get(memory, key).attributes == {"Content-Type": "application/json"}
    fetched = host.get("/workflows/v1/config/guid-1?type=credentials")
    assert fetched.status_code == 200
    assert fetched.json()["data"] == body


def test_a_missing_config_is_404(host: TestClient) -> None:
    assert host.get("/workflows/v1/config/nope?type=credentials").status_code == 404


class _FailingStore:
    """A store that cannot answer: auth, network, a missing bucket."""

    async def get_json(self, key: str) -> dict[str, Any] | None:
        raise obstore.exceptions.PermissionDeniedError("denied")

    async def put_json(self, key: str, body: dict[str, Any]) -> None:
        raise AssertionError("not reached")

    async def put_file(self, key: str, local_path: str) -> None:
        raise AssertionError("not reached")


def test_a_failed_store_read_is_503_not_404() -> None:
    """IAM, network or 5xx: the store is unavailable; the config is not missing."""
    client = TestClient(
        build_asgi_app(StaticHandler(), app_name="mysql", object_store=_FailingStore()),
        raise_server_exceptions=False,
    )
    response = client.get("/workflows/v1/config/guid-1?type=credentials")
    assert response.status_code == 503
    assert response.json()["detail"] == "Object store unavailable"


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permissions that apply to the test user",
)
def test_an_unreadable_object_raises_rather_than_reads_as_missing(
    tmp_path: Path,
) -> None:
    """Only a missing object is ``None``: any other store error must reach the
    route as an exception, which answers 503."""
    locked = tmp_path / "persistent-artifacts"
    locked.mkdir()
    (locked / "config.json").write_text("{}")
    locked.chmod(0)
    from application_sdk.storage.errors import StorageError

    try:
        store = HostObjectStore(LocalStore(tmp_path), kind="local")
        with pytest.raises(StorageError) as raised:
            asyncio.run(store.get_json("persistent-artifacts/config.json"))
        assert isinstance(raised.value.__cause__, obstore.exceptions.BaseError)
        assert asyncio.run(store.get_json("absent/config.json")) is None
    finally:
        locked.chmod(0o700)


def test_without_a_store_the_routes_answer_503() -> None:
    bare = TestClient(
        build_asgi_app(StaticHandler(), app_name="mysql", object_store=None)
    )
    assert bare.get("/workflows/v1/config/guid-1").status_code == 503
    assert bare.post("/workflows/v1/config/guid-1", json={}).status_code == 503
    upload = bare.post("/workflows/v1/file", files={"file": ("a.csv", b"x,y\n")})
    assert upload.status_code == 503


def test_a_path_traversal_id_is_refused(host: TestClient) -> None:
    assert host.get("/workflows/v1/config/..%2Fsecrets").status_code in (404, 422)


def test_a_file_upload_is_stored_under_its_key(
    host: TestClient, memory: MemoryStore
) -> None:
    response = host.post(
        "/workflows/v1/file",
        files={"file": ("rows.csv", b"a,b\n1,2\n", "text/csv")},
        data={"prefix": "sample_data"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    # On the host every upload stays under the app's own namespace.
    assert data["key"].startswith(
        "persistent-artifacts/apps/mysql/uploads/sample_data/"
    )
    assert data["key"].endswith(".csv")
    assert data["fileSize"] == 8
    # Beside it, the worker's integrity sidecar: the digest of what was stored.
    assert _stored(memory) == {
        data["key"]: b"a,b\n1,2\n",
        data["key"] + ".sha256": hashlib.sha256(b"a,b\n1,2\n").hexdigest().encode(),
    }


def test_an_empty_upload_is_stored(host: TestClient, memory: MemoryStore) -> None:
    response = host.post("/workflows/v1/file", files={"file": ("e.csv", b"")})
    assert response.status_code == 200
    key = response.json()["data"]["key"]
    assert _stored(memory) == {
        key: b"",
        key + ".sha256": hashlib.sha256(b"").hexdigest().encode(),
    }


@pytest.mark.parametrize(
    "prefix",
    [
        "../other",
        "a/../../b",
        "/absolute",
        "a//b",
        "trailing/",
        "x" * 300,
        "abc\n",
        ".",
        "./a",
        "a/./b",
        "a/.",
    ],
)
def test_an_unsafe_upload_prefix_is_refused(
    host: TestClient, memory: MemoryStore, prefix: str
) -> None:
    response = host.post(
        "/workflows/v1/file",
        files={"file": ("rows.csv", b"a,b\n", "text/csv")},
        data={"prefix": prefix},
    )
    assert response.status_code == 400
    assert _stored(memory) == {}


def test_a_prefix_naming_another_app_stays_in_this_apps_namespace(
    host: TestClient, memory: MemoryStore
) -> None:
    response = host.post(
        "/workflows/v1/file",
        files={"file": ("x.csv", b"1\n", "text/csv")},
        data={"prefix": "persistent-artifacts/apps/redshift/credentials"},
    )
    assert response.status_code == 200
    stored = _stored(memory)
    assert len(stored) == 2  # the object and its sidecar
    assert all(
        key.startswith("persistent-artifacts/apps/mysql/uploads/") for key in stored
    )


def _worker(
    stored_key: Callable[[str], str] = lambda key: key,
) -> tuple[TestClient, list[str]]:
    """The routes as the worker registers them: no upload namespace."""
    from fastapi import FastAPI

    from application_sdk.handler.object_routes import (
        ObjectRoutesSource,
        register_object_routes,
    )

    uploaded: list[str] = []

    async def _no_config(*_: object) -> None:
        return None

    async def _upload(key: str, _local_path: str) -> None:
        uploaded.append(key)

    app = FastAPI()
    register_object_routes(
        app,
        ObjectRoutesSource(
            configured=lambda: True,
            load_config=_no_config,
            save_config=_no_config,
            upload_file=_upload,
            stored_key=stored_key,
        ),
    )
    return TestClient(app), uploaded


def _worker_upload(client: TestClient, prefix: str | None) -> Response:
    data = {} if prefix is None else {"prefix": prefix}
    return client.post(
        "/workflows/v1/file",
        files={"file": ("rows.csv", b"a,b\n", "text/csv")},
        data=data,
    )


@pytest.mark.parametrize(
    "prefix",
    [None, "workflow_file_upload", "sample_data/2026-run", "my.app/v1.2"],
)
def test_the_worker_keeps_a_safe_prefix_as_given(prefix: str | None) -> None:
    """The worker does not namespace: its keys stay where its readers look."""
    client, uploaded = _worker()
    response = _worker_upload(client, prefix)
    assert response.status_code == 200
    expected = prefix or "workflow_file_upload"
    (key,) = uploaded
    assert key.startswith(f"{expected}/")
    assert response.json()["data"]["key"] == key


@pytest.mark.parametrize(
    "prefix",
    [
        "../x",
        "../../persistent-artifacts/apps/redshift",
        "a/../b",
        "/abs",
        "a//b",
        "trailing/",
        ".",
        "a/./b",
        "my uploads",
        "x" * 300,
        "abc\n",
    ],
)
def test_the_worker_refuses_an_unsafe_prefix(prefix: str) -> None:
    client, uploaded = _worker()
    response = _worker_upload(client, prefix)
    assert response.status_code == 400
    assert uploaded == []


@pytest.mark.parametrize(
    "prefix",
    [
        "persistent-artifacts/apps/redshift/credentials",
        "persistent-artifacts",
        "artifacts/apps/redshift/workflows/run-1",
        "connection-cache",
    ],
)
def test_the_worker_refuses_a_prefix_in_a_reserved_tree(prefix: str) -> None:
    """Another app's tree in the shared bucket is not the worker's to write."""
    client, uploaded = _worker()
    response = _worker_upload(client, prefix)
    assert response.status_code == 400
    assert uploaded == []


def test_an_upload_the_store_would_rewrite_is_refused() -> None:
    """The check runs on the key the store writes: a store that maps
    ``staging/...`` onto ``...`` (as the worker's ``normalize_key`` strips its
    staging root) must not carry ``staging/artifacts/...`` into ``artifacts/``."""

    def _strip_staging(key: str) -> str:
        return key.removeprefix("staging/")

    client, uploaded = _worker(stored_key=_strip_staging)
    assert _worker_upload(client, "staging/artifacts/apps/other").status_code == 400
    assert uploaded == []
    # A key the store keeps unchanged is still accepted.
    assert _worker_upload(client, "workflow_file_upload").status_code == 200
    assert len(uploaded) == 1


# --- the store from the host's env ------------------------------------------

#: Every name the store reads; each test starts with all of them unset.
_STORE_ENV = (
    "ATLAN_S3_BUCKET",
    "ATLAN_S3_REGION",
    "ATLAN_GCP_BUCKET",
    "ATLAN_GCP_REGION",
    "ATLAN_GCP_HMAC_ACCESS_KEY",
    "ATLAN_GCP_HMAC_SECRET",
    "ATLAN_AZURE_STORAGE_ACCOUNT",
    "ATLAN_AZURE_STORAGE_ACCESS_KEY",
    "ATLAN_AZURE_STORAGE_CONTAINER",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)

#: A syntactically valid account key; never a real one.
_AZURE_KEY = base64.b64encode(b"test-account-key-not-real-000000").decode()


@pytest.fixture(autouse=True)
def _clean_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _STORE_ENV:
        monkeypatch.delenv(name, raising=False)


class _Built:
    """Records each store constructor call; hands back a MemoryStore."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str, dict[str, str] | None, str]] = []
        for kind in ("s3", "azure"):
            monkeypatch.setattr(
                _obstore_config, f"build_{kind}_store", self._make(kind)
            )

    def _make(self, kind: str) -> Any:
        # Record the obstore config the shared translation derives from the
        # settings, so assertions read what obstore would be handed.
        def make(settings: Any, *, label: str, **_: object) -> MemoryStore:
            if kind == "s3":
                name = settings.bucket
                config, _opts = _obstore_config.s3_store_args(settings, {})
            else:
                name = settings.container
                config, _opts = _obstore_config.azure_store_args(settings, {})
            self.calls.append(
                (kind, name, None if config is None else dict(config), label)
            )
            return MemoryStore()

        return make


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> _Built:
    return _Built(monkeypatch)


class _Log:
    """Stands in for the module logger and keeps each formatted line."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def warning(self, msg: str, *args: object, **_: object) -> None:
        self.lines.append(msg % args)

    debug = info = error = warning


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _Log:
    log = _Log()
    monkeypatch.setattr(object_routes, "logger", log)
    return log


def _host_env_on_gcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host's env on a GCP tenant: common-app-server's atlan.yaml aliases
    the atlan-app chart's ``GCP_*`` (atlanhq/atlan
    subcharts/atlan-app/templates/deployment.yaml) as ``ATLAN_GCP_*``, and its
    ``ATLAN_S3_*`` aliases are left unresolved, since the chart injects no
    ``S3_*`` there."""
    monkeypatch.setenv("ATLAN_GCP_BUCKET", "tenant-gcs-bucket")
    monkeypatch.setenv("ATLAN_GCP_REGION", "us-central1")
    monkeypatch.setenv("ATLAN_GCP_HMAC_ACCESS_KEY", "test-access-id")
    monkeypatch.setenv("ATLAN_GCP_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("ATLAN_S3_BUCKET", "$(S3_BUCKET)")
    monkeypatch.setenv("ATLAN_S3_REGION", "$(S3_REGION)")


def _host_env_on_azure_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host's env on an Azure Blob tenant: the chart's ``AZURE_STORAGE_*``
    through the host's ``ATLAN_AZURE_STORAGE_*`` aliases, the bucket aliases
    unresolved."""
    monkeypatch.setenv("ATLAN_AZURE_STORAGE_ACCOUNT", "tenantaccount")
    monkeypatch.setenv("ATLAN_AZURE_STORAGE_ACCESS_KEY", _AZURE_KEY)
    monkeypatch.setenv("ATLAN_AZURE_STORAGE_CONTAINER", "objectstore")
    monkeypatch.setenv("ATLAN_S3_BUCKET", "$(S3_BUCKET)")
    monkeypatch.setenv("ATLAN_GCP_BUCKET", "$(GCP_BUCKET)")


def test_no_store_without_a_bucket_or_account(built: _Built) -> None:
    assert HostObjectStore.from_env() is None
    assert built.calls == []


def test_unprefixed_chart_names_are_not_read(
    monkeypatch: pytest.MonkeyPatch, built: _Built
) -> None:
    """Runtime settings take the ``ATLAN_`` prefix (docs/standards/env-vars.md):
    the chart's bare names reach the store only through the host's aliases."""
    monkeypatch.setenv("S3_BUCKET", "someone-elses-bucket")
    monkeypatch.setenv("GCP_BUCKET", "tenant-gcs-bucket")
    monkeypatch.setenv("GCP_HMAC_ACCESS_KEY", "test-access-id")
    monkeypatch.setenv("GCP_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "tenantaccount")
    monkeypatch.setenv("AZURE_STORAGE_CONTAINER", "objectstore")
    assert HostObjectStore.from_env() is None
    assert built.calls == []


@pytest.mark.parametrize(
    "gcp_alias", [None, "$(GCP_BUCKET)"], ids=["no-alias", "unresolved-alias"]
)
def test_on_aws_the_store_is_s3_with_the_ambient_identity(
    monkeypatch: pytest.MonkeyPatch, built: _Built, gcp_alias: str | None
) -> None:
    if gcp_alias is not None:
        monkeypatch.setenv("ATLAN_GCP_BUCKET", gcp_alias)
        monkeypatch.setenv("ATLAN_GCP_HMAC_ACCESS_KEY", "$(GCP_HMAC_ACCESS_KEY)")
        monkeypatch.setenv("ATLAN_GCP_HMAC_SECRET", "$(GCP_HMAC_SECRET)")
    monkeypatch.setenv("ATLAN_AZURE_STORAGE_ACCOUNT", "$(AZURE_STORAGE_ACCOUNT)")
    monkeypatch.setenv("ATLAN_S3_BUCKET", "tenant-bucket")
    monkeypatch.setenv("ATLAN_S3_REGION", "eu-west-1")

    store = HostObjectStore.from_env()

    assert repr(store) == "HostObjectStore(s3)"
    # The ambient identity: a bucket and a region, no endpoint, no keys.
    assert built.calls == [
        ("s3", "tenant-bucket", {"aws_region": "eu-west-1"}, "host-s3")
    ]


def test_without_atlan_s3_region_the_pods_aws_region_is_used(
    monkeypatch: pytest.MonkeyPatch, built: _Built
) -> None:
    """As the worker's binding resolves it: obstore given a config reads no
    AWS_REGION itself and would default to us-east-1."""
    monkeypatch.setenv("ATLAN_S3_BUCKET", "tenant-bucket")
    monkeypatch.setenv("ATLAN_S3_REGION", "$(S3_REGION)")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    HostObjectStore.from_env()
    assert built.calls[0][2] == {"aws_region": "ap-south-1"}


def test_on_gcp_the_store_is_gcs_through_its_s3_api(
    monkeypatch: pytest.MonkeyPatch, built: _Built
) -> None:
    _host_env_on_gcp(monkeypatch)

    store = HostObjectStore.from_env()

    assert repr(store) == "HostObjectStore(gcs)"
    assert built.calls == [
        (
            "s3",
            "tenant-gcs-bucket",
            {
                "aws_endpoint": "https://storage.googleapis.com",
                "aws_access_key_id": "test-access-id",
                "aws_secret_access_key": "test-secret",
                "aws_disable_tagging": "true",
                "aws_region": "us-central1",
            },
            "host-gcs",
        )
    ]


def test_the_gcs_store_obstore_builds_sends_no_checksums_or_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """boto3 >= 1.36 sent CRC32 checksums GCS rejects (boto/boto3#4400). obstore
    adds one only when ``checksum_algorithm`` is configured, and the store
    disables the ``x-amz-tagging`` header GCS also rejects."""
    _host_env_on_gcp(monkeypatch)
    store = HostObjectStore.from_env()
    assert store is not None
    config = store._store.config  # type: ignore[attr-defined]
    assert config["endpoint"] == "https://storage.googleapis.com"
    assert config["disable_tagging"] == "true"
    assert "checksum_algorithm" not in config


def test_gcp_without_its_hmac_keys_has_no_store(
    monkeypatch: pytest.MonkeyPatch, built: _Built, log: _Log
) -> None:
    """Never the ambient S3 store instead; the warning names the variables and
    carries no key material."""
    _host_env_on_gcp(monkeypatch)
    monkeypatch.setenv("ATLAN_GCP_HMAC_SECRET", "")

    assert HostObjectStore.from_env() is None
    assert built.calls == []
    assert len(log.lines) == 1 and "ATLAN_GCP_HMAC_SECRET" in log.lines[0]
    assert "test-access-id" not in log.lines[0]


def test_on_azure_blob_the_store_is_the_tenants_container(
    monkeypatch: pytest.MonkeyPatch, built: _Built
) -> None:
    _host_env_on_azure_blob(monkeypatch)

    store = HostObjectStore.from_env()

    assert repr(store) == "HostObjectStore(azure)"
    assert built.calls == [
        (
            "azure",
            "objectstore",
            {
                "azure_storage_account_name": "tenantaccount",
                "azure_storage_account_key": _AZURE_KEY,
            },
            "host-azure",
        )
    ]


def test_azure_without_an_account_key_uses_the_pods_identity(
    monkeypatch: pytest.MonkeyPatch, built: _Built
) -> None:
    """No key in the config: obstore resolves the pod's ambient identity
    (workload identity on the chart's Azure pods)."""
    _host_env_on_azure_blob(monkeypatch)
    monkeypatch.setenv("ATLAN_AZURE_STORAGE_ACCESS_KEY", "$(AZURE_STORAGE_ACCESS_KEY)")
    HostObjectStore.from_env()
    assert built.calls[0][2] == {"azure_storage_account_name": "tenantaccount"}


def test_azure_without_a_container_has_no_store_and_logs_no_key(
    monkeypatch: pytest.MonkeyPatch, built: _Built, log: _Log
) -> None:
    _host_env_on_azure_blob(monkeypatch)
    monkeypatch.setenv("ATLAN_AZURE_STORAGE_CONTAINER", "")
    assert HostObjectStore.from_env() is None
    assert built.calls == []
    assert len(log.lines) == 1 and "ATLAN_AZURE_STORAGE_CONTAINER" in log.lines[0]
    assert _AZURE_KEY not in log.lines[0]


def test_a_bucket_wins_over_an_azure_account(
    monkeypatch: pytest.MonkeyPatch, built: _Built
) -> None:
    """As in the chart: an Azure tenant with S3 enabled gets S3."""
    _host_env_on_azure_blob(monkeypatch)
    monkeypatch.setenv("ATLAN_S3_BUCKET", "tenant-bucket")
    HostObjectStore.from_env()
    assert [call[0] for call in built.calls] == ["s3"]


def test_without_obstore_there_is_no_store(
    monkeypatch: pytest.MonkeyPatch, log: _Log
) -> None:
    def _missing(*_: object, **__: object) -> None:
        raise ModuleNotFoundError("No module named 'obstore'", name="obstore")

    monkeypatch.setattr(_obstore_config, "build_s3_store", _missing)
    monkeypatch.setenv("ATLAN_S3_BUCKET", "tenant-bucket")
    assert HostObjectStore.from_env() is None
    assert "atlan-application-sdk-api[obstore]" in log.lines[0]


def test_a_store_obstore_refuses_is_no_store_and_logs_no_key(
    monkeypatch: pytest.MonkeyPatch, log: _Log
) -> None:
    """obstore's message quotes what it rejected; only the type is logged."""
    _host_env_on_azure_blob(monkeypatch)
    monkeypatch.setenv("ATLAN_AZURE_STORAGE_ACCESS_KEY", "not base64 secret-ish")
    assert HostObjectStore.from_env() is None
    assert len(log.lines) == 1 and "GenericError" in log.lines[0]
    assert "secret-ish" not in log.lines[0] and "Invalid" not in log.lines[0]


def test_a_built_stores_repr_and_config_log_carry_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: list[str] = []

    class _ConfigLog(_Log):
        def info(self, msg: str, *args: object, **_: object) -> None:
            lines.append(msg % args)

    monkeypatch.setattr(_obstore_config, "logger", _ConfigLog())
    for configure, secret in (
        (_host_env_on_gcp, "test-secret"),
        (_host_env_on_azure_blob, _AZURE_KEY),
    ):
        for name in _STORE_ENV:
            monkeypatch.delenv(name, raising=False)
        configure(monkeypatch)
        store = HostObjectStore.from_env()
        assert store is not None
        assert secret not in repr(store)
        assert secret not in repr(store._store)
    assert lines and not any(
        secret in line for line in lines for secret in ("test-secret", _AZURE_KEY)
    )


# --- the host's default store ----------------------------------------------


@pytest.fixture
def fresh_default_store() -> Iterator[None]:
    """``default_object_store`` is cached per process: read the env afresh."""
    default_object_store.cache_clear()
    yield
    default_object_store.cache_clear()


#: The host on an Azure Blob tenant whose manifest does not alias the chart's
#: AZURE_STORAGE_*: its other aliases left unresolved.
_UNALIASED_AZURE_BLOB_HOST_ENV = {
    "AZURE_BLOB": "true",
    "AZURE_STORAGE_CONTAINER": "objectstore",
    "ATLAN_S3_BUCKET": "$(S3_BUCKET)",
    "ATLAN_S3_REGION": "$(S3_REGION)",
    "ATLAN_GCP_BUCKET": "$(GCP_BUCKET)",
    "ATLAN_GCP_REGION": "$(GCP_REGION)",
    "ATLAN_AZURE_STORAGE_ACCOUNT": "$(AZURE_STORAGE_ACCOUNT)",
    "ATLAN_AZURE_STORAGE_CONTAINER": "$(AZURE_STORAGE_CONTAINER)",
}


@pytest.mark.usefixtures("fresh_default_store")
@pytest.mark.parametrize(
    "env",
    [{}, _UNALIASED_AZURE_BLOB_HOST_ENV],
    ids=["no-store", "unresolved-aliases"],
)
def test_without_a_store_the_host_answers_503(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    host = TestClient(build_asgi_app(StaticHandler(), app_name="mysql"))
    assert host.get("/workflows/v1/config/g1?type=credentials").status_code == 503
    assert (
        host.post("/workflows/v1/config/g1?type=credentials", json={}).status_code
        == 503
    )
    upload = host.post("/workflows/v1/file", files={"file": ("a.csv", b"x,y\n")})
    assert upload.status_code == 503


@pytest.mark.usefixtures("fresh_default_store")
def test_the_hosts_default_store_reads_its_azure_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_env_on_azure_blob(monkeypatch)
    assert repr(default_object_store()) == "HostObjectStore(azure)"


def test_building_the_store_loads_no_sql_client() -> None:
    """The store's client options read the custom CA through
    ``application_sdk.common.ssl_utils``. Anything under ``application_sdk.clients``
    runs that package's ``__init__``, which loads the SQL client and its
    ``python-dotenv`` import, absent from a core-only api install."""
    import subprocess

    probe = (
        "import sys\n"
        "from application_sdk.handler.object_routes import HostObjectStore\n"
        "from application_sdk import _obstore_config\n"
        "_obstore_config.obstore_client_options()\n"
        "print(sorted(m for m in sys.modules if m.startswith('application_sdk.clients')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert out.stdout.strip() == "[]"


# --- the shared storage seam: sidecar, gateway retry, classified failures ----

# The shapes object_store renders a backend HTTP failure in. Hosts synthetic.
_GATEWAY_NO_ROUTE = (
    "Generic S3 error: Error performing GET "
    "https://gateway.example.invalid/api/blobstorage/k in 8ms - "
    "Server returned non-2xx status code: 404 Not Found: "
    '{"message":"no Route matched with those values"}'
)
_GATEWAY_AUTH_UNAVAILABLE = (
    "Generic S3 error: Error performing PUT "
    "https://gateway.example.invalid/api/blobstorage/k in 8ms - "
    "Server returned non-2xx status code: 401 Unauthorized: "
    '{"code": 1005, "error": "invalid_client", "message": "x"}'
)


def _failing_writer(put: Callable[..., Any]) -> Callable[..., Any]:
    """``obstore.open_writer_async`` (the upload's multipart writer) failing as
    the async *put* does: it is entered with ``async with``, not awaited."""

    class _Writer:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._call = (args, kwargs)

        async def __aenter__(self) -> Any:
            args, kwargs = self._call
            return await put(*args, **kwargs)

        async def __aexit__(self, *_: object) -> None:
            return None

    return _Writer


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the gateway retry's backoff instead of sleeping through it."""
    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    from application_sdk.storage import ops

    # The host's requests go through the worker's store seam, and its retry.
    monkeypatch.setattr(ops, "_retry_sleep", _sleep)
    return slept


def test_an_uploaded_file_verifies_through_the_workers_download(
    host: TestClient, memory: MemoryStore, tmp_path: Path
) -> None:
    """The host's sidecar is the one the worker's download checks."""
    from application_sdk.storage import ops

    payload = b"id,name\n1,a\n"
    key = host.post(
        "/workflows/v1/file", files={"file": ("rows.csv", payload, "text/csv")}
    ).json()["data"]["key"]
    target = tmp_path / "rows.csv"
    digest = asyncio.run(ops.download_file(key, target, memory, compute_hash=True))
    assert digest == hashlib.sha256(payload).hexdigest()
    # A corrupted object now fails the worker's check against the host's sidecar.
    obstore.put(memory, key, b"id,name\n1,b\n")
    from application_sdk.storage.errors import StorageIntegrityError

    with pytest.raises(StorageIntegrityError):
        asyncio.run(ops.download_file(key, tmp_path / "again.csv", memory))


def test_sidecars_off_writes_only_the_object(
    host: TestClient, memory: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("application_sdk.constants.STORAGE_WRITE_SIDECARS", False)
    response = host.post("/workflows/v1/file", files={"file": ("a.csv", b"x\n")})
    assert response.status_code == 200
    assert list(_stored(memory)) == [response.json()["data"]["key"]]


def test_a_failed_sidecar_write_still_stores_the_upload(
    host: TestClient, memory: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_put = obstore.put_async

    async def _put(store: Any, key: str, *args: Any, **kwargs: Any) -> Any:
        if key.endswith(".sha256"):
            raise RuntimeError("sidecar refused")
        return await real_put(store, key, *args, **kwargs)

    monkeypatch.setattr(obstore, "put_async", _put)
    response = host.post("/workflows/v1/file", files={"file": ("a.csv", b"x\n")})
    assert response.status_code == 200
    assert _stored(memory) == {response.json()["data"]["key"]: b"x\n"}


def test_a_gateway_no_route_read_is_retried(
    host: TestClient, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    """The gateway's not-yet-routed 404 is retried, as on the worker, and is
    never read as a missing config."""
    body = {"host": "db.example"}
    assert host.post("/workflows/v1/config/g?type=credentials", json=body).is_success
    real_get = obstore.get_async
    failures = iter([RuntimeError(_GATEWAY_NO_ROUTE)] * 2)

    async def _get(store: Any, key: str, *args: Any, **kwargs: Any) -> Any:
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return await real_get(store, key, *args, **kwargs)

    monkeypatch.setattr(obstore, "get_async", _get)
    fetched = host.get("/workflows/v1/config/g?type=credentials")
    assert fetched.status_code == 200
    assert fetched.json()["data"] == body
    assert len(no_sleep) == 2


def test_a_gateway_that_stays_unrouted_is_503_not_404(
    host: TestClient, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    async def _get(*_: Any, **__: Any) -> Any:
        raise RuntimeError(_GATEWAY_NO_ROUTE)

    monkeypatch.setattr(obstore, "get_async", _get)
    response = host.get("/workflows/v1/config/g?type=credentials")
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Object store gateway is not ready; retry shortly"
    )
    assert len(no_sleep) == 3


def test_a_missing_bucket_read_is_503_not_404(
    host: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 the listing cannot confirm is the bucket, not the config."""

    async def _get(*_: Any, **__: Any) -> Any:
        raise FileNotFoundError("object missing")

    async def _list(*_: Any, **__: Any) -> Any:
        raise RuntimeError("Server returned non-2xx status code: 404 NoSuchBucket")

    monkeypatch.setattr(obstore, "get_async", _get)
    monkeypatch.setattr(obstore, "list_with_delimiter_async", _list)
    response = host.get("/workflows/v1/config/g?type=credentials")
    assert response.status_code == 503
    assert response.json()["detail"] == "Object store unavailable"


@pytest.mark.parametrize(
    ("message", "detail"),
    [
        (
            _GATEWAY_AUTH_UNAVAILABLE,
            "Object store gateway could not verify the request; retry shortly",
        ),
        (
            "Server returned non-2xx status code: 404 Not Found: "
            "<Error><Code>ContainerNotFound</Code></Error>",
            "Object store container does not exist",
        ),
        ("connection reset by peer", "Object store unavailable"),
    ],
)
def test_a_failed_write_is_a_classified_503(
    host: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    detail: str,
) -> None:
    """Config writes and uploads answer the shared table's verdict, not a 500."""

    async def _put(*_: Any, **__: Any) -> Any:
        raise RuntimeError(message)

    # A config is one PUT; an upload goes through the worker's multipart writer.
    monkeypatch.setattr(obstore, "put_async", _put)
    monkeypatch.setattr(obstore, "open_writer_async", _failing_writer(_put))
    config = host.post("/workflows/v1/config/g?type=credentials", json={"a": 1})
    upload = host.post("/workflows/v1/file", files={"file": ("a.csv", b"x\n")})
    for response in (config, upload):
        assert response.status_code == 503
        assert response.json()["detail"] == detail


def test_an_upload_named_like_a_sidecar_is_stored_as_data(
    host: TestClient, memory: MemoryStore, tmp_path: Path
) -> None:
    """An ``x.sha256`` upload must not take a bookkeeping key: the worker would
    neither verify it nor list it as data."""
    from application_sdk.storage import ops

    payload = b"not a digest\n"
    response = host.post("/workflows/v1/file", files={"file": ("x.sha256", payload)})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["extension"] == "sha256"
    key = data["key"]
    assert not key.endswith(".sha256")
    assert _stored(memory)[key + ".sha256"] == (
        hashlib.sha256(payload).hexdigest().encode()
    )
    obstore.put(memory, key, b"tampered\n")
    from application_sdk.storage.errors import StorageIntegrityError

    with pytest.raises(StorageIntegrityError):
        asyncio.run(ops.download_file(key, tmp_path / "x", memory))


def test_a_store_failure_logs_no_driver_text(
    host: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """obstore's message can quote a signed URL or the store's credential: the
    host logs the classified evidence and the type, never the text or traceback."""
    from unittest.mock import MagicMock

    secret = "https://b.example.invalid/k?X-Amz-Signature=deadbeef"

    async def _put(*_: Any, **__: Any) -> Any:
        raise RuntimeError(f"Server returned non-2xx status code: 403 {secret}")

    log = MagicMock()
    monkeypatch.setattr(object_routes, "logger", log)
    monkeypatch.setattr(obstore, "put_async", _put)
    monkeypatch.setattr(obstore, "open_writer_async", _failing_writer(_put))
    assert (
        host.post("/workflows/v1/file", files={"file": ("a.csv", b"x")}).status_code
        == 503
    )
    assert (
        host.post("/workflows/v1/config/g?type=credentials", json={}).status_code == 503
    )
    assert log.warning.call_count == 2
    for call in log.warning.call_args_list:
        assert "exc_info" not in call.kwargs
        assert all(secret not in str(arg) for arg in call.args)
        assert 403 in call.args


def test_a_failed_sidecar_write_logs_no_driver_text(
    host: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    secret = "account_key=c2VjcmV0"
    real_put = obstore.put_async

    async def _put(store: Any, key: str, *args: Any, **kwargs: Any) -> Any:
        if key.endswith(".sha256"):
            raise RuntimeError(secret)
        return await real_put(store, key, *args, **kwargs)

    from application_sdk.storage import integrity

    # The sidecar write is the worker's (storage.integrity), so is its log.
    log = MagicMock()
    monkeypatch.setattr(integrity, "logger", log)
    monkeypatch.setattr(obstore, "put_async", _put)
    assert (
        host.post("/workflows/v1/file", files={"file": ("a.csv", b"x")}).status_code
        == 200
    )
    (call,) = log.warning.call_args_list
    assert "exc_info" not in call.kwargs
    assert all(secret not in str(arg) for arg in call.args)
    assert "RuntimeError" in call.args


def test_on_the_host_a_failed_upload_event_carries_no_driver_text(
    host: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's per-attempt storage event copies the driver's (redacted)
    message for its OTLP pipeline; on an api-only install it carries only the
    error class, as the host's own failure log does."""
    from unittest.mock import MagicMock

    from application_sdk.storage import ops

    signed = "https://b.example.invalid/k?X-Goog-Signature=deadbeef"

    async def _put(*_: Any, **__: Any) -> Any:
        raise RuntimeError(f"Server returned non-2xx status code: 403 {signed}")

    log = MagicMock()
    monkeypatch.setattr(ops, "logger", log)
    monkeypatch.setattr(ops, "api_only_install", lambda: True)
    monkeypatch.setattr(obstore, "open_writer_async", _failing_writer(_put))
    response = host.post("/workflows/v1/file", files={"file": ("a.csv", b"x")})
    assert response.status_code == 503
    (call,) = log.log.call_args_list
    assert call.kwargs["extra"]["error_class"] == "RuntimeError"
    assert "error_message" not in call.kwargs["extra"]
    assert signed not in str(call)


def test_an_upload_is_size_checked_after_the_write(
    host: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's post-write check runs on the host: a store that reports a
    different size than was sent fails the upload instead of answering 200."""
    real_head = obstore.head_async

    async def _head(store: Any, key: str, *args: Any, **kwargs: Any) -> Any:
        meta = dict(await real_head(store, key, *args, **kwargs))
        meta["size"] = 0
        return meta

    monkeypatch.setattr(obstore, "head_async", _head)
    response = host.post("/workflows/v1/file", files={"file": ("a.csv", b"x,y\n")})
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("given", "stored"),
    [("dir\\file.csv", "dir/file.csv"), ("/dir/file.csv/", "dir/file.csv")],
)
def test_a_direct_put_file_normalises_its_key(
    memory: MemoryStore, tmp_path: Path, given: str, stored: str
) -> None:
    """``put_file`` is public: a direct caller's key is normalised as it always
    was, so the object (and its sidecar) sit where the worker's download looks."""
    from application_sdk.storage import ops

    local = tmp_path / "f.csv"
    local.write_bytes(b"a,b\n")
    asyncio.run(HostObjectStore(memory, kind="memory").put_file(given, str(local)))
    assert set(_stored(memory)) == {stored, stored + ".sha256"}
    digest = asyncio.run(
        ops.download_file(given, tmp_path / "back.csv", memory, compute_hash=True)
    )
    assert digest == hashlib.sha256(b"a,b\n").hexdigest()
