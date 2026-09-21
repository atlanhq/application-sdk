"""The /start selector becomes a Temporal workflow TYPE, so it cannot be free text.

`entrypoint` is interpolated into ``f"{app_name}:{entrypoint}"`` and dispatched
onto the app's real task queue. Unvalidated, an unauthenticated POST got a 200
with a real run_id for a type no worker registers: the execution is created, its
workflow task fails and retries forever, and the tenant's Temporal namespace
fills with stuck executions an operator has to find and terminate.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from server_sdk.handler.base import DefaultHandler
from server_sdk.server import build_asgi_app


class _RecordingStarter:
    """Matches the WorkflowStarter protocol: async start(request) -> StartResult."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    async def start(self, request):
        self.dispatched.append(f"{request.app_name}:{request.entrypoint}")
        from server_sdk.workflow import StartResult

        return StartResult(workflow_id=request.workflow_id or "wf-1", run_id="run-1")


@pytest.fixture
def started():
    starter = _RecordingStarter()
    client = TestClient(
        build_asgi_app(DefaultHandler(), app_name="redshift", workflow_starter=starter),
        raise_server_exceptions=False,
    )
    return client, starter


@pytest.mark.parametrize(
    "entrypoint",
    ["../../etc/passwd", "9leading-digit", "has space", "semi;colon", "", "a/b"],
    ids=["traversal", "leading-digit", "space", "semicolon", "empty", "slash"],
)
def test_a_malformed_entrypoint_is_rejected_before_dispatch(
    started, entrypoint
) -> None:
    client, starter = started
    resp = client.post(f"/workflows/v1/start?entrypoint={entrypoint}", json={})
    assert resp.status_code == 400, resp.text
    assert starter.dispatched == [], "nothing may reach Temporal"


def test_a_well_formed_entrypoint_still_dispatches(started) -> None:
    client, starter = started
    resp = client.post("/workflows/v1/start?entrypoint=crawler", json={})
    assert resp.status_code == 200, resp.text
    assert starter.dispatched == ["redshift:crawler"]


def test_the_rejection_matches_the_other_entrypoint_guards(started) -> None:
    """One wording across the package — server.py and manifest.py say the same."""
    client, _ = started
    resp = client.post("/workflows/v1/start?entrypoint=../x", json={})
    assert resp.json()["detail"] == "Invalid entrypoint name"


# ── the selector has three sources; all three must be guarded ───────────────


@pytest.mark.parametrize(
    "entrypoint", ["../../etc/passwd", "9leading", "has space", "a/b"]
)
def test_the_legacy_workflow_type_field_cannot_bypass_the_guard(
    started, entrypoint
) -> None:
    """Guarding only ?entrypoint left the deprecated `workflow_type` body field
    carrying the same value into the same dispatch — a 200 with a real run_id."""
    client, starter = started
    resp = client.post("/workflows/v1/start", json={"workflow_type": entrypoint})
    assert resp.status_code == 400, resp.text
    assert starter.dispatched == []


def test_a_well_formed_legacy_workflow_type_still_works(started) -> None:
    client, starter = started
    resp = client.post("/workflows/v1/start", json={"workflow_type": "crawler"})
    assert resp.status_code == 200, resp.text
    assert starter.dispatched == ["redshift:crawler"]


def test_a_malformed_default_entrypoint_is_caught_too(started) -> None:
    """App-configured rather than caller-supplied, but a 400 here beats a stuck
    execution later."""
    from server_sdk.handler.base import DefaultHandler
    from server_sdk.server import build_asgi_app

    _, starter = started
    client = TestClient(
        build_asgi_app(
            DefaultHandler(),
            app_name="redshift",
            workflow_starter=starter,
            default_entrypoint="../bad",
        ),
        raise_server_exceptions=False,
    )
    assert client.post("/workflows/v1/start", json={}).status_code == 400
