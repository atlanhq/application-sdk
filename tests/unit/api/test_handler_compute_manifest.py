"""``Handler.compute_manifest``: a manifest computed per request.

The worker's handler service and the consolidated API host both run it, so a
manifest that depends on ``fe_inputs`` or on the tenant is served the same from
either place, with the task queue stamped as for a generated manifest.
"""

from __future__ import annotations

import json
import sys
import types
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from application_sdk.errors.leaves import InvalidInputError
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    ManifestInput,
    MetadataInput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)
from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.metadata_routes import handler_compute_manifest

_GENERATED = {
    "execution_mode": "automation-engine",
    "dag": {
        "extract": {
            "activity_name": "execute_workflow",
            "inputs": {"task_queue": "atlan-parity-{deployment_name}", "args": {}},
        }
    },
}


class _StaticHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[])


class _ComputingHandler(_StaticHandler):
    """Computes ``computed``; serves every other entry point as generated."""

    async def compute_manifest(self, input: ManifestInput) -> dict[str, Any] | None:
        if input.entrypoint != "computed":
            return None
        return {
            "execution_mode": input.manifest["execution_mode"],
            "dag": {
                "run": {
                    "activity_name": "execute_workflow",
                    "inputs": {
                        "task_queue": f"atlan-{self.context.app_name}-{{deployment_name}}",
                        "args": {"policy_id": input.fe_inputs.get("policy_id", "")},
                    },
                }
            },
            "triggers": {"http": {"enabled": True}},
        }


class _FailingHandler(_StaticHandler):
    async def compute_manifest(self, input: ManifestInput) -> dict[str, Any] | None:
        raise RuntimeError("catalog lookup failed for token sk-do-not-leak")


class _RefusingHandler(_StaticHandler):
    """Answers the caller itself: its detail is written for them."""

    async def compute_manifest(self, input: ManifestInput) -> dict[str, Any] | None:
        raise HTTPException(status_code=422, detail="policy_id is required")


class _InvalidInputHandler(_StaticHandler):
    async def compute_manifest(self, input: ManifestInput) -> dict[str, Any] | None:
        raise InvalidInputError(
            message="policy_id is not a valid policy", field="policy_id"
        )


@pytest.fixture
def generated(tmp_path: Path) -> Path:
    gen = tmp_path / "generated"
    for entrypoint in ("computed", "static"):
        (gen / entrypoint).mkdir(parents=True)
        (gen / entrypoint / "manifest.json").write_text(json.dumps(_GENERATED))
    return gen


def _clients(
    handler: Handler, generated: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, TestClient]:
    import application_sdk.handler.service as svc_module
    from application_sdk.handler.service import create_app_handler_service

    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "parity")
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")
    monkeypatch.setenv("ATLAN_APPENV__PARITY__ATLAN_DEPLOYMENT_NAME", "prod")
    monkeypatch.setattr(svc_module, "CONTRACT_GENERATED_DIR", generated)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        worker = create_app_handler_service(handler, app_name="parity")
    host = build_asgi_app(handler, app_name="parity", generated_dir=generated)
    return (
        TestClient(worker, raise_server_exceptions=False),
        TestClient(host, raise_server_exceptions=False),
    )


_FE_INPUTS = {"policy_id": "4e1c7a52-0000-4000-8000-000000000001"}

# Every request names an entry point: with none, the worker resolves the default
# from its app registry, which a bare handler does not populate (see
# test_metadata_route_parity.py).
REQUESTS = [
    ("GET", "/workflows/v1/manifest?entrypoint=computed", None),
    (
        "GET",
        "/workflows/v1/manifest?entrypoint=computed&fe_inputs="
        + quote(json.dumps(_FE_INPUTS)),
        None,
    ),
    (
        "POST",
        "/workflows/v1/manifest",
        {"entrypoint": "computed", "fe_inputs": _FE_INPUTS},
    ),
    ("POST", "/manifest", {"entrypoint": "computed", "fe_inputs": _FE_INPUTS}),
    ("POST", "/manifest", {"entrypoint": "static"}),
    ("GET", "/manifest?entrypoint=missing", None),
]


@pytest.mark.parametrize(("method", "path", "body"), REQUESTS)
def test_worker_and_host_serve_a_computed_manifest_identically(
    generated: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: object,
) -> None:
    worker, host = _clients(_ComputingHandler(), generated, monkeypatch)
    w = worker.request(method, path, json=body)
    h = host.request(method, path, json=body)
    assert (w.status_code, w.content) == (h.status_code, h.content)


def test_the_computed_manifest_gets_the_queue_the_worker_polls(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, host = _clients(_ComputingHandler(), generated, monkeypatch)
    manifest = host.post(
        "/manifest", json={"entrypoint": "computed", "fe_inputs": _FE_INPUTS}
    ).json()
    inputs = manifest["dag"]["run"]["inputs"]
    assert inputs["task_queue"] == "atlan-parity-prod"
    assert inputs["args"] == {"policy_id": _FE_INPUTS["policy_id"]}
    assert manifest["triggers"] == {"http": {"enabled": True}}


def test_none_serves_the_generated_manifest_unchanged(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, computing = _clients(_ComputingHandler(), generated, monkeypatch)
    _, static = _clients(_StaticHandler(), generated, monkeypatch)
    body = {"entrypoint": "static"}
    assert computing.post("/manifest", json=body).content == (
        static.post("/manifest", json=body).content
    )


def test_a_handler_without_the_override_resolves_no_hook() -> None:
    resolve = handler_compute_manifest(_StaticHandler(), lambda: "parity")
    assert resolve("computed") is None


@pytest.mark.parametrize(
    ("handler", "status", "detail"),
    [
        (_RefusingHandler, 422, "policy_id is required"),
        (_InvalidInputHandler, 400, "policy_id is not a valid policy"),
    ],
)
@pytest.mark.parametrize("surface", [0, 1], ids=["worker", "host"])
def test_a_deliberate_compute_manifest_error_reaches_the_caller(
    generated: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: int,
    handler: type[Handler],
    status: int,
    detail: str,
) -> None:
    """An HTTPException or typed AppError answers as on the other handler routes."""
    client = _clients(handler(), generated, monkeypatch)[surface]
    response = client.post("/manifest", json={"entrypoint": "computed"})
    assert (response.status_code, response.json()["detail"]) == (status, detail)


@pytest.mark.parametrize("surface", [0, 1], ids=["worker", "host"])
def test_a_failing_compute_manifest_is_a_generic_500(
    generated: Path, monkeypatch: pytest.MonkeyPatch, surface: int
) -> None:
    client = _clients(_FailingHandler(), generated, monkeypatch)[surface]
    response = client.post("/manifest", json={"entrypoint": "computed"})
    assert response.status_code == 500
    assert response.json()["detail"] == "Internal server error"
    assert "sk-do-not-leak" not in response.text


@pytest.mark.parametrize("surface", [0, 1], ids=["worker", "host"])
def test_oversized_get_fe_inputs_is_refused_before_the_handler_runs(
    generated: Path, monkeypatch: pytest.MonkeyPatch, surface: int
) -> None:
    client = _clients(_ComputingHandler(), generated, monkeypatch)[surface]
    big = quote(json.dumps({"policy_id": "x" * 9000}))
    response = client.get(f"/manifest?entrypoint=computed&fe_inputs={big}")
    assert response.status_code == 413


def test_a_core_hook_still_wins_on_the_worker(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing ``app.<segment>.core.compute_manifest`` apps are unchanged."""

    async def compute_manifest(
        manifest: dict[str, Any], fe_inputs: dict[str, Any]
    ) -> dict[str, Any]:
        return {**manifest, "from": "core"}

    for name in ("app", "app.computed"):
        package = types.ModuleType(name)
        package.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, package)
    core = types.ModuleType("app.computed.core")
    core.compute_manifest = compute_manifest  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.computed.core", core)

    worker, host = _clients(_ComputingHandler(), generated, monkeypatch)
    body = {"entrypoint": "computed"}
    assert worker.post("/manifest", json=body).json()["from"] == "core"
    assert "run" in host.post("/manifest", json=body).json()["dag"]
