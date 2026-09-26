"""No credential material may reach Temporal history.

`/start` hands its body to the starter, which passes it as the workflow
argument — so anything left in it is written into workflow history in plaintext
and stays there for the retention period. The route strips one key,
`credentials`, and relies on normalize_credentials' documented promise that
"credential material ends up **only** under ``credentials`` and nowhere else in
the body".

Only one of that function's four return paths actually kept it: a body carrying
BOTH a credentials key and v2 flat top-level keys kept the flat ones, and a
scalar `credentials` matched no branch at all and kept everything.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from server_sdk.handler.base import DefaultHandler
from server_sdk.handler.contracts import normalize_credentials
from server_sdk.server import build_asgi_app
from server_sdk.workflow import StartResult

SECRET = "hunter2-REAL-SECRET"


class _Recorder:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def start(self, request):
        self.bodies.append(request.body)
        return StartResult(workflow_id=request.workflow_id or "w", run_id="r")


@pytest.fixture
def started():
    starter = _Recorder()
    client = TestClient(
        build_asgi_app(DefaultHandler(), app_name="postgres", workflow_starter=starter),
        raise_server_exceptions=False,
    )
    return client, starter


@pytest.mark.parametrize(
    ("name", "body"),
    [
        (
            "v3 list plus flat keys",
            {"credentials": [], "host": "db", "password": SECRET},
        ),
        (
            "populated v3 list plus flat",
            {"credentials": [{"key": "host", "value": "db"}], "password": SECRET},
        ),
        (
            "nested dict plus flat",
            {"credentials": {"username": "svc"}, "password": SECRET},
        ),
        (
            "extra beside a v3 list",
            {"credentials": [], "extra": {"private_key": SECRET}},
        ),
        ("pure flat", {"host": "db", "password": SECRET}),
        ("pure v3 list", {"credentials": [{"key": "password", "value": SECRET}]}),
        ("scalar credentials", {"credentials": "some-guid", "password": SECRET}),
        ("empty-string credentials", {"credentials": "", "password": SECRET}),
        ("zero credentials", {"credentials": 0, "password": SECRET}),
    ],
)
def test_no_shape_reaches_the_workflow_argument(started, name: str, body: dict) -> None:
    client, starter = started
    resp = client.post("/workflows/v1/start?entrypoint=crawler", json=dict(body))
    assert resp.status_code == 200, resp.text
    forwarded = json.dumps(starter.bodies[-1])
    assert SECRET not in forwarded, f"{name}: {forwarded}"


# ── the invariant itself, at the source ─────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"credentials": [], "host": "h", "password": "p"},
        {"credentials": {"username": "u"}, "password": "p"},
        {"credentials": "guid", "password": "p"},
        {"credentials": 0, "password": "p"},
        {"host": "h", "password": "p"},
        {},
    ],
)
def test_no_credential_key_survives_outside_credentials(body: dict) -> None:
    out = normalize_credentials(dict(body))
    leftover = {
        k
        for k in out
        if k != "credentials" and k in {"host", "password", "username", "extra"}
    }
    assert not leftover, leftover


def test_non_credential_keys_are_preserved() -> None:
    """Stripping must not eat the workflow's actual arguments."""
    out = normalize_credentials(
        {"credentials": [], "entrypoint": "crawler", "metadata": {"x": 1}}
    )
    assert out["entrypoint"] == "crawler"
    assert out["metadata"] == {"x": 1}


@pytest.mark.parametrize(
    ("body", "expected_keys"),
    [
        ({"host": "h", "password": "p"}, {"host", "password"}),
        ({"credentials": {"host": "h", "password": "p"}}, {"host", "password"}),
    ],
)
def test_credentials_are_still_resolved(body: dict, expected_keys: set) -> None:
    out = normalize_credentials(dict(body))
    assert {pair["key"] for pair in out["credentials"]} == expected_keys
