"""``POST /workflows/v1/validate`` on the consolidated API host (FND-501).

Creation-time validation must not depend on the app's worker being up, so the
route ships in the api distribution and the host serves it from the app's
generated contract, the same model ``/input-contract`` publishes.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.base import DefaultHandler

_PACKAGE = "fnd501_validate_host_app"

_INPUT_MODULE = textwrap.dedent(
    """
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.templates.contracts import ExtractionInput


    class AppInputContract(ExtractionInput):
        oauth_client_id: str
        enable_tags: bool = False
        source_credential: CredentialRef | None = None
    """
)


class _NoopHandler(DefaultHandler):
    pass


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    generated = tmp_path / _PACKAGE / "generated"
    generated.mkdir(parents=True)
    (tmp_path / _PACKAGE / "__init__.py").write_text("")
    (generated / "__init__.py").write_text("")
    (generated / "_input.py").write_text(_INPUT_MODULE)
    # A flat manifest makes this a single-entry-point app: no ?entrypoint=.
    (generated / "manifest.json").write_text("{}")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield TestClient(
            build_asgi_app(
                _NoopHandler(),
                app_name="validate-host",
                app_package=_PACKAGE,
                generated_dir=generated,
                object_store=None,
            ),
            raise_server_exceptions=False,
        )
    finally:
        for name in [m for m in sys.modules if m.startswith(_PACKAGE)]:
            del sys.modules[name]


def test_host_accepts_a_config_that_fits(host: TestClient) -> None:
    response = host.post(
        "/workflows/v1/validate",
        json={
            "oauth_client_id": "client-123",
            "include_filter": {"^db$": ["^public$"]},
            # Inline credentials are stripped, never judged as configuration.
            "credentials": {"username": "u", "password": "p"},
            "workflow_id": "wf-1",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


def test_host_rejects_a_missing_required_field(host: TestClient) -> None:
    response = host.post("/workflows/v1/validate", json={"enable_tags": True})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert [d["field"] for d in body["detail"]] == ["oauth_client_id"]
    assert body["detail"][0]["type"] == "missing"


def test_host_runs_the_contracts_field_validators(host: TestClient) -> None:
    """What the JSON Schema cannot express is still enforced, without echo."""
    response = host.post(
        "/workflows/v1/validate",
        json={
            "oauth_client_id": "client-123",
            "include_filter": {"^db$": ["^SECRET_SCHEMA["]},
        },
    )

    assert response.status_code == 422
    assert [d["field"] for d in response.json()["detail"]] == ["include_filter"]
    assert "SECRET_SCHEMA" not in response.text


def test_host_validates_against_the_published_contract(host: TestClient) -> None:
    """The verdict is on the model ``/input-contract`` serves."""
    schema = host.get("/workflows/v1/input-contract").json()

    assert "oauth_client_id" in schema["required"]


def test_host_rejects_a_non_object_body(host: TestClient) -> None:
    response = host.post("/workflows/v1/validate", json=["not", "an", "object"])

    assert response.status_code == 422
