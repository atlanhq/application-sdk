"""The config and file routes on the consolidated API host.

Heracles writes a credential's non-secret fields to the app's config on every
credential create and update and before every run submit, and the app's worker
reads them back at run time. On the host the write must land on exactly the
key the worker reads, under the hosted app's name — never the host's own.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

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
from application_sdk.handler.object_routes import S3ObjectStore


class _Handler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[])


class _NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3:
    """The three boto3 S3 client calls the store makes, over a dict."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        try:
            return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}
        except KeyError:
            raise _NoSuchKey() from None

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str):
        self.objects[(Bucket, Key)] = Body

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        with open(filename, "rb") as f:
            self.objects[(bucket, key)] = f.read()


@pytest.fixture
def s3() -> _FakeS3:
    return _FakeS3()


@pytest.fixture
def host(s3: _FakeS3) -> TestClient:
    store = S3ObjectStore("tenant-bucket", client=s3)
    return TestClient(
        build_asgi_app(_Handler(), app_name="mysql", object_store=store),
        raise_server_exceptions=False,
    )


def test_a_credential_config_lands_where_the_apps_worker_reads_it(
    host: TestClient, s3: _FakeS3
) -> None:
    body = {"host": "db.example", "port": 3306, "authType": "basic"}
    response = host.post("/workflows/v1/config/guid-1?type=credentials", json=body)
    assert response.status_code == 200
    key = "persistent-artifacts/apps/mysql/credentials/guid-1/config.json"
    assert ("tenant-bucket", key) in s3.objects
    fetched = host.get("/workflows/v1/config/guid-1?type=credentials")
    assert fetched.status_code == 200
    assert fetched.json()["data"] == body


def test_a_missing_config_is_404(host: TestClient) -> None:
    assert host.get("/workflows/v1/config/nope?type=credentials").status_code == 404


class _AccessDenied(Exception):
    response = {"Error": {"Code": "AccessDenied"}}


class _DenyingS3(_FakeS3):
    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        raise _AccessDenied()


def test_a_failed_store_read_is_503_not_404() -> None:
    """IAM, network or 5xx: the store is unavailable; the config is not missing."""
    store = S3ObjectStore("tenant-bucket", client=_DenyingS3())
    client = TestClient(
        build_asgi_app(_Handler(), app_name="mysql", object_store=store),
        raise_server_exceptions=False,
    )
    response = client.get("/workflows/v1/config/guid-1?type=credentials")
    assert response.status_code == 503
    assert response.json()["detail"] == "Object store unavailable"


def test_without_a_store_the_routes_answer_503() -> None:
    bare = TestClient(build_asgi_app(_Handler(), app_name="mysql", object_store=None))
    assert bare.get("/workflows/v1/config/guid-1").status_code == 503
    assert bare.post("/workflows/v1/config/guid-1", json={}).status_code == 503
    upload = bare.post("/workflows/v1/file", files={"file": ("a.csv", b"x,y\n")})
    assert upload.status_code == 503


def test_a_path_traversal_id_is_refused(host: TestClient) -> None:
    assert host.get("/workflows/v1/config/..%2Fsecrets").status_code in (404, 422)


def test_a_file_upload_is_stored_under_its_key(host: TestClient, s3: _FakeS3) -> None:
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
    assert s3.objects[("tenant-bucket", data["key"])] == b"a,b\n1,2\n"


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
    host: TestClient, s3: _FakeS3, prefix: str
) -> None:
    response = host.post(
        "/workflows/v1/file",
        files={"file": ("rows.csv", b"a,b\n", "text/csv")},
        data={"prefix": prefix},
    )
    assert response.status_code == 400
    assert s3.objects == {}


def test_a_prefix_naming_another_app_stays_in_this_apps_namespace(
    host: TestClient, s3: _FakeS3
) -> None:
    response = host.post(
        "/workflows/v1/file",
        files={"file": ("x.csv", b"1\n", "text/csv")},
        data={"prefix": "persistent-artifacts/apps/redshift/credentials"},
    )
    assert response.status_code == 200
    ((bucket, key),) = s3.objects
    assert key.startswith("persistent-artifacts/apps/mysql/uploads/")


def test_the_store_comes_from_atlan_s3_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATLAN_S3_BUCKET", raising=False)
    monkeypatch.delenv("GCP_BUCKET", raising=False)
    assert S3ObjectStore.from_env() is None


def test_an_unprefixed_s3_bucket_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime settings take the ``ATLAN_`` prefix (docs/standards/env-vars.md):
    a pod's generic ``S3_BUCKET`` for something else must not become this store."""
    monkeypatch.delenv("ATLAN_S3_BUCKET", raising=False)
    monkeypatch.delenv("GCP_BUCKET", raising=False)
    monkeypatch.setenv("S3_BUCKET", "someone-elses-bucket")
    assert S3ObjectStore.from_env() is None


def test_the_store_reads_the_atlan_names(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        S3ObjectStore,
        "__init__",
        lambda self, bucket, region=None, client=None: built.append((bucket, region)),
    )
    monkeypatch.delenv("GCP_BUCKET", raising=False)
    monkeypatch.setenv("ATLAN_S3_BUCKET", "tenant-bucket")
    monkeypatch.setenv("ATLAN_S3_REGION", "eu-west-1")
    assert S3ObjectStore.from_env() is not None
    assert built == [("tenant-bucket", "eu-west-1")]


def _boto3_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Record each ``boto3.client`` call; the store gets a fake client."""
    import boto3

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _client(*args: Any, **kwargs: Any) -> _FakeS3:
        calls.append((args, kwargs))
        return _FakeS3()

    monkeypatch.setattr(boto3, "client", _client)
    return calls


def _host_env_on_gcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host's env on a GCP tenant: ``GCP_*`` as the atlan-app chart injects
    them (atlanhq/atlan subcharts/atlan-app/templates/deployment.yaml), and the
    ``ATLAN_S3_*`` aliases from common-app-server's atlan.yaml left unresolved,
    since the chart injects no ``S3_*`` there."""
    monkeypatch.setenv("GCP_BUCKET", "tenant-gcs-bucket")
    monkeypatch.setenv("GCP_REGION", "us-central1")
    monkeypatch.setenv("GCP_HMAC_ACCESS_KEY", "test-access-id")
    monkeypatch.setenv("GCP_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("ATLAN_S3_BUCKET", "$(S3_BUCKET)")
    monkeypatch.setenv("ATLAN_S3_REGION", "$(S3_REGION)")


def test_on_aws_the_client_is_built_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _boto3_clients(monkeypatch)
    monkeypatch.delenv("GCP_BUCKET", raising=False)
    monkeypatch.setenv("ATLAN_S3_BUCKET", "tenant-bucket")
    monkeypatch.setenv("ATLAN_S3_REGION", "eu-west-1")

    store = S3ObjectStore.from_env()

    assert store is not None and store._bucket == "tenant-bucket"
    # The ambient identity: no endpoint, no keys, no client config.
    assert calls == [(("s3",), {"region_name": "eu-west-1"})]


def test_on_gcp_the_store_is_gcs_through_its_s3_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _boto3_clients(monkeypatch)
    _host_env_on_gcp(monkeypatch)

    store = S3ObjectStore.from_env()

    assert store is not None and store._bucket == "tenant-gcs-bucket"
    ((args, kwargs),) = calls
    config = kwargs.pop("config")
    assert args == ("s3",)
    assert kwargs == {
        "endpoint_url": "https://storage.googleapis.com",
        "region_name": "us-central1",
        "aws_access_key_id": "test-access-id",
        "aws_secret_access_key": "test-secret",
    }
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"


class _Log:
    """Stands in for the module logger and keeps each formatted line."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def warning(self, msg: str, *args: object, **_: object) -> None:
        self.lines.append(msg % args)

    debug = info = error = warning


def test_gcp_without_its_hmac_keys_has_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never the ambient AWS client instead; the warning names the variables and
    carries no key material."""
    calls = _boto3_clients(monkeypatch)
    log = _Log()
    monkeypatch.setattr(object_routes, "logger", log)
    _host_env_on_gcp(monkeypatch)
    monkeypatch.setenv("GCP_HMAC_SECRET", "")

    assert S3ObjectStore.from_env() is None
    assert calls == []
    assert len(log.lines) == 1 and "GCP_HMAC_SECRET" in log.lines[0]
    assert "test-access-id" not in log.lines[0]


def _checksums(headers: Mapping[str, Any]) -> dict[str, Any]:
    """The request headers asking the store to compute or verify a checksum."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower().startswith(("x-amz-checksum", "x-amz-sdk-checksum"))
        or name.lower() == "x-amz-trailer"
        or (name.lower() == "content-encoding" and "aws-chunked" in str(value))
    }


class _Raw:
    """A response body botocore can read."""

    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def stream(self, **_: object) -> Iterator[bytes]:
        yield self._body.read()

    def read(self, *args: Any, **_: object) -> bytes:
        return self._body.read(*args)


def _answer_here(client: Any) -> list[Any]:
    """Answer *client*'s requests in-process, never on the network; return them."""
    from botocore.awsrequest import AWSResponse

    sent: list[Any] = []

    def _send(request: Any, **_: object) -> AWSResponse:
        sent.append(request)
        body = b'{"host": "db.example"}' if request.method == "GET" else b""
        headers = {"Content-Length": str(len(body))}
        return AWSResponse(request.url, 200, headers, _Raw(body))

    client.meta.events.register("before-send.s3", _send)
    return sent


def test_gcs_requests_carry_no_checksums(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """boto3 >= 1.36 adds CRC32 checksums GCS rejects (boto/boto3#4400): the
    real client the store builds must send none, on writes, uploads or reads."""
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    for name in (
        "AWS_REQUEST_CHECKSUM_CALCULATION",
        "AWS_RESPONSE_CHECKSUM_VALIDATION",
    ):
        monkeypatch.delenv(name, raising=False)
    _host_env_on_gcp(monkeypatch)
    store = S3ObjectStore.from_env()
    assert store is not None
    sent = _answer_here(store._client)
    host = TestClient(build_asgi_app(_Handler(), app_name="mysql", object_store=store))

    body = {"host": "db.example"}
    assert (
        host.post("/workflows/v1/config/g1?type=credentials", json=body).status_code
        == 200
    )
    assert host.get("/workflows/v1/config/g1?type=credentials").json()["data"] == body
    upload = host.post("/workflows/v1/file", files={"file": ("a.csv", b"x,y\n")})
    assert upload.status_code == 200

    assert [r.method for r in sent] == ["PUT", "GET", "PUT"]
    assert {urlsplit(r.url).hostname for r in sent} == {"storage.googleapis.com"}
    assert [_checksums(r.headers) for r in sent] == [{}, {}, {}]

    # Not vacuous: boto3's default client does send them.
    import boto3

    plain = boto3.client(
        "s3",
        endpoint_url="https://storage.googleapis.com",
        region_name="us-central1",
        aws_access_key_id="test-access-id",
        aws_secret_access_key="test-secret",
    )
    control = _answer_here(plain)
    plain.put_object(Bucket="tenant-gcs-bucket", Key="k", Body=b"{}")
    assert _checksums(control[0].headers)


@pytest.fixture
def fresh_default_store() -> Iterator[None]:
    """``default_object_store`` is cached per process: read the env afresh."""
    default_object_store.cache_clear()
    yield
    default_object_store.cache_clear()


#: The host on an Azure Blob tenant: the chart's Blob-mode env (atlanhq/atlan
#: atlan-app deployment.yaml) and the host's ATLAN_S3_* aliases left unresolved.
_AZURE_BLOB_HOST_ENV = {
    "AZURE_BLOB": "true",
    "AZURE_STORAGE_CONTAINER": "objectstore",
    "ATLAN_S3_BUCKET": "$(S3_BUCKET)",
    "ATLAN_S3_REGION": "$(S3_REGION)",
}


@pytest.mark.usefixtures("fresh_default_store")
@pytest.mark.parametrize(
    "env", [{}, _AZURE_BLOB_HOST_ENV], ids=["no-bucket", "azure-blob"]
)
def test_without_a_bucket_the_host_answers_503(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for name in ("ATLAN_S3_BUCKET", "GCP_BUCKET"):
        monkeypatch.delenv(name, raising=False)
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
