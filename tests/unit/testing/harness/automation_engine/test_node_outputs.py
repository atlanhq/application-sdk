"""``AEClient.get_node_outputs``: one node's outputs from AE's run read (FND-3571)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from application_sdk.testing.harness.automation_engine import AEClient
from application_sdk.testing.harness.automation_engine._errors import AtlanApiHttpError

_OUTPUTS = {"passed": True, "observations": []}


async def _read(status: int, body: Any) -> dict[str, Any]:
    client = AEClient("https://tenant.example.com", "tok")
    calls: list[tuple[str, str]] = []

    async def _request(method: str, path: str, **_: Any) -> tuple[int, Any]:
        calls.append((method, path))
        return status, body

    with patch.object(client, "_request", side_effect=_request):
        outputs = await client.get_node_outputs("run/1", "sdk-store-assert")
    assert calls == [("GET", "/automation/api/v1/runs/run%2F1")]
    return outputs


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"dag": {"sdk-store-assert": {"outputs": _OUTPUTS}}}, id="bare"),
        pytest.param(
            {"data": {"dag": {"sdk-store-assert": {"outputs": _OUTPUTS}}}},
            id="data-envelope",
        ),
    ],
)
async def test_returns_the_nodes_outputs(body: dict[str, Any]) -> None:
    assert await _read(200, body) == _OUTPUTS


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(404, {"detail": "no run"}, id="http-error"),
        pytest.param(200, "not json", id="non-object"),
        pytest.param(200, {"dag": {}}, id="node-missing"),
        pytest.param(200, {"dag": {"sdk-store-assert": {}}}, id="outputs-missing"),
        pytest.param(
            200, {"dag": {"sdk-store-assert": {"outputs": None}}}, id="outputs-null"
        ),
    ],
)
async def test_an_unanswerable_read_raises_rather_than_returning_empty(
    status: int, body: Any
) -> None:
    with pytest.raises(AtlanApiHttpError):
        await _read(status, body)
