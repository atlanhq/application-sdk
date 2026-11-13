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
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import obstore
import pytest
from fastapi.testclient import TestClient
from obstore.store import LocalStore, MemoryStore

from application_sdk import _obstore_config
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
    object_routes,
)
from application_sdk.handler.asgi import build_asgi_app, default_object_store
from application_sdk.handler.object_routes import HostObjectStore


class _Handler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[])


def _stored(store: MemoryStore) -> dict[str, bytes]:
    """Every object in *store*, by key."""
    keys = [meta["path"] for batch in obstore.list(store) for meta in batch]
    return {key: bytes(obstore.get(store, key).bytes()) for key in keys}


@pytest.fixture
def memory() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def host(memory: MemoryStore) -> TestClient:
    store = HostObjectStore(memory, kind="memory")
    return TestClient(
        build_asgi_app(_Handler(), app_name="mysql", object_store=store),
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
        build_asgi_app(_Handler(), app_name="mysql", object_store=_FailingStore()),
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
    try:
        store = HostObjectStore(LocalStore(tmp_path), kind="local")
        with pytest.raises(obstore.exceptions.BaseError):
            asyncio.run(store.get_json("persistent-artifacts/config.json"))
        assert asyncio.run(store.get_json("absent/config.json")) is None
    finally:
        locked.chmod(0o700)


def test_without_a_store_the_routes_answer_503() -> None:
    bare = TestClient(build_asgi_app(_Handler(), app_name="mysql", object_store=None))
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
    assert _stored(memory) == {data["key"]: b"a,b\n1,2\n"}


def test_an_empty_upload_is_stored(host: TestClient, memory: MemoryStore) -> None:
    response = host.post("/workflows/v1/file", files={"file": ("e.csv", b"")})
    assert response.status_code == 200
    assert _stored(memory) == {response.json()["data"]["key"]: b""}


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
    (key,) = _stored(memory)
    assert key.startswith("persistent-artifacts/apps/mysql/uploads/")


def test_the_worker_keeps_accepting_any_prefix() -> None:
    """Only the shared host validates the prefix; a worker (no upload namespace)
    stores it as given, exactly as before."""
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
        ),
    )
    response = TestClient(app).post(
        "/workflows/v1/file",
        files={"file": ("rows.csv", b"a,b\n", "text/csv")},
        data={"prefix": "my uploads/./2026 run"},
    )
    assert response.status_code == 200
    assert uploaded[0].startswith("my uploads/./2026 run/")


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
            monkeypatch.setattr(_obstore_config, f"make_{kind}_store", self._make(kind))

    def _make(self, kind: str) -> Any:
        def make(
            name: str, config: dict[str, str] | None = None, *, label: str, **_: object
        ) -> MemoryStore:
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

    monkeypatch.setattr(_obstore_config, "make_s3_store", _missing)
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
    host = TestClient(build_asgi_app(_Handler(), app_name="mysql"))
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
