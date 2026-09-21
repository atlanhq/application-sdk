"""The manifest route, which decides which task queue a caller submits onto.

A manifest declares its queue as "atlan-{app_name}-{deployment_name}". Serving
either token unsubstituted names a queue no worker polls, so the submit reports
success and the workflow then sits there forever — the silent failure the
migration guide calls out for a process-global application name.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server_sdk.manifest import register_manifest_routes

QUEUE = "atlan-{app_name}-{deployment_name}"


@pytest.fixture(autouse=True)
def _deployment(monkeypatch):
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")


@pytest.fixture
def gen(tmp_path):
    def _write(**entrypoints):
        for ep, body in entrypoints.items():
            d = tmp_path / ep
            d.mkdir()
            (d / "manifest.json").write_text(json.dumps(body))
        return tmp_path

    return _write


def _client(directory, app_name="redshift") -> TestClient:
    app = FastAPI()
    register_manifest_routes(app, app_name=app_name, generated_dir=directory)
    return TestClient(app, raise_server_exceptions=False)


def _queue(resp) -> str:
    return resp.json()["dag"]["extract"]["task_queue"]


def test_both_tokens_are_substituted(gen) -> None:
    d = gen(crawler={"dag": {"extract": {"task_queue": QUEUE}}})
    resp = _client(d).get("/workflows/v1/manifest?entrypoint=crawler")
    assert resp.status_code == 200
    assert _queue(resp) == "atlan-redshift-prod"


def test_a_stale_manifest_is_reported(gen, caplog: pytest.LogCaptureFixture) -> None:
    d = gen(crawler={"dag": {"extract": {"task_queue": QUEUE}}})
    with caplog.at_level(logging.WARNING):
        _client(d).get("/workflows/v1/manifest?entrypoint=crawler")
    assert "app_name" in caplog.text


def test_a_manifest_without_the_token_logs_nothing(
    gen, caplog: pytest.LogCaptureFixture
) -> None:
    d = gen(crawler={"dag": {"extract": {"task_queue": "atlan-redshift-prod"}}})
    with caplog.at_level(logging.WARNING):
        resp = _client(d).get("/workflows/v1/manifest?entrypoint=crawler")
    assert _queue(resp) == "atlan-redshift-prod"
    assert "app_name" not in caplog.text


def test_the_app_name_used_is_the_one_registered(gen) -> None:
    """Not a process-global — the host serves many apps from one process."""
    d = gen(crawler={"dag": {"extract": {"task_queue": QUEUE}}})
    resp = _client(d, app_name="postgres").get("/workflows/v1/manifest?entrypoint=crawler")
    assert _queue(resp) == "atlan-postgres-prod"


def test_no_entrypoint_falls_back_alphabetically(gen) -> None:
    d = gen(
        crawler={"dag": {"extract": {"task_queue": QUEUE}}},
        miner={"dag": {"extract": {"task_queue": "atlan-miner-x"}}},
    )
    resp = _client(d).get("/workflows/v1/manifest")
    assert resp.status_code == 200
    assert _queue(resp) == "atlan-redshift-prod"


def test_a_malformed_entrypoint_is_400_not_a_path_read(gen) -> None:
    d = gen(crawler={"dag": {}})
    resp = _client(d).get("/workflows/v1/manifest?entrypoint=../../etc/passwd")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid entrypoint name"


def test_an_unknown_entrypoint_is_404(gen) -> None:
    d = gen(crawler={"dag": {}})
    assert _client(d).get("/workflows/v1/manifest?entrypoint=nope").status_code == 404
