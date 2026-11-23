"""The request-body cap on the worker's handler app and the consolidated host.

Without a cap one upload could fill the pod's ephemeral storage (Starlette
spools a multipart file to disk before the route runs) and one JSON body was
read whole, several times over. Both apps install the cap through the shared
``register_object_routes``; an oversize body gets 413 whether or not it
declares its length, and an upload over the cap leaves no file on disk and
nothing in the store.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import obstore
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from obstore.store import MemoryStore

from application_sdk.handler import object_routes
from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.object_routes import (
    MAX_BODY_BYTES_ENV,
    MAX_UPLOAD_BYTES_ENV,
    HostObjectStore,
    RequestBodyLimitMiddleware,
)
from application_sdk.handler.service import create_app_handler_service
from tests.unit.handler._service_support import _TestHandler

_BODY_CAP = 1024
_UPLOAD_CAP = 8 * 1024


@pytest.fixture(autouse=True)
def _small_caps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv(MAX_BODY_BYTES_ENV, str(_BODY_CAP))
    monkeypatch.setenv(MAX_UPLOAD_BYTES_ENV, str(_UPLOAD_CAP))
    # Every temp file -- Starlette's spool and the route's own -- lands here.
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    return spool


@pytest.fixture
def memory() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def worker() -> TestClient:
    app = create_app_handler_service(_TestHandler(), app_name="test-app")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def host(memory: MemoryStore) -> TestClient:
    store = HostObjectStore(memory, kind="memory")
    app = build_asgi_app(_TestHandler(), app_name="mysql", object_store=store)
    return TestClient(app, raise_server_exceptions=False)


def _chunks(total: int, size: int = 512) -> Iterator[bytes]:
    """A body sent chunked: no Content-Length for the fast path to read."""
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        yield b"a" * n


def _oversize_json() -> bytes:
    return b'{"credentials": [], "pad": "' + b"x" * (_BODY_CAP * 2) + b'"}'


def _multipart(payload_size: int) -> tuple[bytes, str]:
    boundary = "capboundary"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="big.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode()
        + b"a" * payload_size
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _stored(store: MemoryStore) -> list[str]:
    return [meta["path"] for batch in obstore.list(store) for meta in batch]


# --- JSON routes -----------------------------------------------------------


@pytest.mark.parametrize("client_name", ["worker", "host"])
def test_an_oversize_auth_body_gets_413(
    client_name: str, request: pytest.FixtureRequest
) -> None:
    client: TestClient = request.getfixturevalue(client_name)
    response = client.post(
        "/workflows/v1/auth",
        content=_oversize_json(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


@pytest.mark.parametrize("client_name", ["worker", "host"])
def test_an_oversize_auth_body_without_a_length_gets_413(
    client_name: str, request: pytest.FixtureRequest
) -> None:
    """No Content-Length: the cap trips while reading, inside ``read_json_object``,
    which must let it through rather than answer 422."""
    client: TestClient = request.getfixturevalue(client_name)
    response = client.post(
        "/workflows/v1/auth",
        content=_chunks(_BODY_CAP * 2),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_a_body_under_the_cap_is_served(worker: TestClient) -> None:
    response = worker.post(
        "/workflows/v1/auth", json={"credentials": [], "connection_id": "c"}
    )
    assert response.status_code == 200


def test_an_oversize_config_post_gets_413_and_stores_nothing(
    host: TestClient, memory: MemoryStore
) -> None:
    response = host.post(
        "/workflows/v1/config/abc?type=credentials",
        content=_chunks(_BODY_CAP * 2),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert _stored(memory) == []


# --- the upload route -------------------------------------------------------


@pytest.mark.parametrize("client_name", ["worker", "host"])
@pytest.mark.parametrize("declare_length", [True, False])
def test_an_oversize_upload_gets_413_and_leaves_no_temp_file(
    client_name: str,
    declare_length: bool,
    request: pytest.FixtureRequest,
    _small_caps: Path,
) -> None:
    client: TestClient = request.getfixturevalue(client_name)
    body, content_type = _multipart(_UPLOAD_CAP * 2)
    response = client.post(
        "/workflows/v1/file",
        content=body if declare_length else _chunks(len(body)),
        headers={"content-type": content_type},
    )
    assert response.status_code == 413
    assert list(_small_caps.iterdir()) == []


def test_an_oversize_upload_stores_nothing(
    host: TestClient, memory: MemoryStore
) -> None:
    body, content_type = _multipart(_UPLOAD_CAP * 2)
    response = host.post(
        "/workflows/v1/file",
        content=_chunks(len(body)),
        headers={"content-type": content_type},
    )
    assert response.status_code == 413
    assert _stored(memory) == []


def test_an_upload_gets_its_own_larger_cap(
    host: TestClient, memory: MemoryStore, _small_caps: Path
) -> None:
    """Over the JSON cap, under the upload cap: stored, and the temp file is gone."""
    response = host.post(
        "/workflows/v1/file", files={"file": ("a.csv", b"a" * (_BODY_CAP * 4))}
    )
    assert response.status_code == 200
    assert len(_stored(memory)) == 1
    assert list(_small_caps.iterdir()) == []


# --- configuration -----------------------------------------------------------


def _installed(app: FastAPI) -> list[dict[str, int]]:
    return [
        dict(m.kwargs)  # type: ignore[arg-type]
        for m in app.user_middleware
        if m.cls is RequestBodyLimitMiddleware
    ]


def test_both_apps_install_the_cap_once(worker: TestClient, host: TestClient) -> None:
    for client in (worker, host):
        app = client.app
        assert isinstance(app, FastAPI)
        assert _installed(app) == [
            {"max_body_bytes": _BODY_CAP, "max_upload_bytes": _UPLOAD_CAP}
        ]


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-5"])
def test_a_bad_cap_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(MAX_BODY_BYTES_ENV, raw)
    monkeypatch.setenv(MAX_UPLOAD_BYTES_ENV, raw)
    app = FastAPI()
    object_routes.install_body_limit(app)
    assert _installed(app) == [
        {
            "max_body_bytes": object_routes.DEFAULT_MAX_BODY_BYTES,
            "max_upload_bytes": object_routes.DEFAULT_MAX_UPLOAD_BYTES,
        }
    ]


def test_the_upload_cap_applies_under_a_mount() -> None:
    """The host mounts each app under a prefix; the route is matched without it."""
    middleware = RequestBodyLimitMiddleware(
        FastAPI(), max_body_bytes=1, max_upload_bytes=2
    )
    scope = {"path": "/apps/mysql/workflows/v1/file", "root_path": "/apps/mysql"}
    assert middleware._limit_for(scope) == 2
    assert middleware._limit_for({"path": "/workflows/v1/auth"}) == 1
