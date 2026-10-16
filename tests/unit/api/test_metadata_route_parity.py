"""The worker and the API host serve an app's generated files identically.

Both register :func:`application_sdk.handler.metadata_routes.register_metadata_routes`:
the worker over ``CONTRACT_GENERATED_DIR``, the host over the ``generated/``
folder ``gen_app_api.py`` ships inside the app's package. For the same files and
the same deployment, the configmaps and the manifest (task queue included) must
be byte-for-byte equal, or a run submitted through the host would target a queue
the app's worker does not poll.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

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
from application_sdk.handler.asgi import app_env, build_asgi_app

_MANIFEST = {
    "execution_mode": "native",
    "dag": {
        "extract": {
            "activity_name": "extract",
            "task_queue": "atlan-parity-{deployment_name}",
        }
    },
}


class _Handler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[])


@pytest.fixture
def generated(tmp_path: Path) -> Path:
    gen = tmp_path / "generated"
    gen.mkdir()
    (gen / "manifest.json").write_text(json.dumps(_MANIFEST))
    (gen / "parity.json").write_text(json.dumps({"config": {"properties": {"a": 1}}}))
    (gen / "atlan-connectors-parity.json").write_text(
        json.dumps({"config": {"cred": True}, "defaultConnectorType": "parity"})
    )
    return gen


@pytest.fixture
def clients(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, TestClient]:
    import application_sdk.handler.service as svc_module
    from application_sdk.handler.service import create_app_handler_service

    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "parity")
    # The worker reads its own bare name; the host only the app's prefixed copy.
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")
    monkeypatch.setenv("ATLAN_APPENV__PARITY__ATLAN_DEPLOYMENT_NAME", "prod")
    monkeypatch.setattr(svc_module, "CONTRACT_GENERATED_DIR", generated)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        worker = create_app_handler_service(_Handler(), app_name="parity")
    host = build_asgi_app(_Handler(), app_name="parity", generated_dir=generated)
    return (
        TestClient(worker, raise_server_exceptions=False),
        TestClient(host, raise_server_exceptions=False),
    )


# An id that names no file falls back to the app's default entry point. The
# worker resolves that from its app registry, which this bare handler does not
# populate, so it is checked on the host alone below
# (test_the_host_resolves_an_app_id_to_the_single_form) and against a real
# app's worker end to end.
REQUESTS = [
    ("GET", "/workflows/v1/configmaps", None),
    ("GET", "/workflows/v1/configmap/parity", None),
    ("GET", "/workflows/v1/configmap/atlan-connectors-parity", None),
    ("GET", "/workflows/v1/manifest", None),
    ("GET", "/workflows/v1/manifest?entrypoint=extract-metadata", None),
    ("GET", "/workflows/v1/manifest?entrypoint=bad%20name", None),
    ("POST", "/workflows/v1/manifest", {"entrypoint": "extract-metadata"}),
    ("POST", "/workflows/v1/manifest", [1, 2]),
    ("GET", "/manifest", None),
    ("POST", "/manifest", {}),
]


@pytest.mark.parametrize(("method", "path", "body"), REQUESTS)
def test_worker_and_host_answer_identically(
    clients: tuple[TestClient, TestClient], method: str, path: str, body: object
) -> None:
    worker, host = clients
    w = worker.request(method, path, json=body)
    h = host.request(method, path, json=body)
    assert (w.status_code, w.content) == (h.status_code, h.content)


def test_the_served_task_queue_is_the_one_the_worker_polls(
    clients: tuple[TestClient, TestClient],
) -> None:
    _, host = clients
    manifest = host.post("/manifest", json={}).json()
    assert manifest["dag"]["extract"]["task_queue"] == "atlan-parity-prod"


def test_a_per_app_deployment_name_wins_on_the_host(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "host-wide")
    monkeypatch.setenv("ATLAN_APPENV__PARITY__ATLAN_DEPLOYMENT_NAME", "prod")
    assert app_env("parity", "ATLAN_DEPLOYMENT_NAME") == "prod"
    host = TestClient(
        build_asgi_app(_Handler(), app_name="parity", generated_dir=generated)
    )
    manifest = host.get("/workflows/v1/manifest").json()
    assert manifest["dag"]["extract"]["task_queue"] == "atlan-parity-prod"


def test_the_host_resolves_an_app_id_to_the_single_form(
    clients: tuple[TestClient, TestClient],
) -> None:
    _, host = clients
    response = host.get("/workflows/v1/configmap/atlan-parity")
    assert response.status_code == 200
    config = json.loads(response.json()["data"]["data"]["config"])
    assert config == {"properties": {"a": 1}}


def test_without_generated_files_the_host_serves_no_metadata_routes() -> None:
    host = TestClient(build_asgi_app(_Handler(), app_name="bare", app_package="app"))
    assert host.get("/workflows/v1/configmaps").status_code == 404


def test_the_workers_queue_override_is_stamped_on_the_host(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")
    monkeypatch.setenv("ATLAN_APPENV__PARITY__ATLAN_TASK_QUEUE", "custom-queue")
    host = TestClient(
        build_asgi_app(_Handler(), app_name="parity", generated_dir=generated)
    )
    manifest = host.post("/manifest", json={}).json()
    assert manifest["dag"]["extract"]["task_queue"] == "custom-queue"


def test_the_host_never_reads_its_own_bare_queue_settings(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAN_TASK_QUEUE", "host-wide-queue")
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "host-wide")
    monkeypatch.setenv("ATLAN_APPENV__PARITY__ATLAN_DEPLOYMENT_NAME", "prod")
    assert app_env("parity", "ATLAN_TASK_QUEUE") == ""
    host = TestClient(
        build_asgi_app(_Handler(), app_name="parity", generated_dir=generated)
    )
    manifest = host.post("/manifest", json={}).json()
    assert manifest["dag"]["extract"]["task_queue"] == "atlan-parity-prod"


def test_without_the_apps_own_settings_the_host_stamps_no_queue(
    generated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAN_TASK_QUEUE", "host-wide-queue")
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "host-wide")
    host = TestClient(
        build_asgi_app(_Handler(), app_name="parity", generated_dir=generated)
    )
    manifest = host.post("/manifest", json={}).json()
    queue = manifest["dag"]["extract"]["task_queue"]
    assert "host-wide" not in queue
