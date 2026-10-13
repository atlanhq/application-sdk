"""The config and file routes on the consolidated API host.

Heracles writes a credential's non-secret fields to the app's config on every
credential create and update and before every run submit, and the app's worker
reads them back at run time. On the host the write must land on exactly the
key the worker reads, under the hosted app's name — never the host's own.
"""

from __future__ import annotations

import io
from typing import Any

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
)
from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.object_routes import S3ObjectStore, config_key


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
    assert data["key"].startswith("sample_data/") and data["key"].endswith(".csv")
    assert s3.objects[("tenant-bucket", data["key"])] == b"a,b\n1,2\n"


def test_the_host_key_is_the_workers_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import application_sdk.constants as constants
    from application_sdk.handler.service import _config_objectstore_key

    monkeypatch.setattr(constants, "APPLICATION_NAME", "mysql")
    assert _config_objectstore_key("guid-1", "credentials") == config_key(
        "mysql", "guid-1", "credentials"
    )


def test_the_store_comes_from_s3_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S3_BUCKET", raising=False)
    assert S3ObjectStore.from_env() is None
