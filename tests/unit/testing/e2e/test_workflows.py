"""Unit tests for the handler-service workflow helpers.

This module had no test file before FND-241. The test that earns its keep is
:func:`test_one_tunnel_serves_the_whole_wait`: the poll opened and tore down a
``kubectl port-forward`` on *every* probe, and nothing pinned that it no longer
does — a regression there is invisible in a unit suite and expensive in a live
run.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from application_sdk.testing.e2e.workflows import run_workflow, wait_for_workflow


def _response(payload: dict[str, Any], status: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json = MagicMock(return_value=payload)
    response.raise_for_status = MagicMock()
    return response


class _RecordingSession:
    """A port-forward session that answers a scripted sequence of statuses."""

    def __init__(self, *responses: MagicMock) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def request(
        self, method: str, path: str, *, body: Any = None, headers: Any = None
    ) -> MagicMock:
        self.calls.append((method, path))
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


@asynccontextmanager
async def _one_session(session: _RecordingSession, opens: list[int]):
    opens.append(1)
    yield session


@pytest.mark.asyncio
async def test_run_workflow_posts_the_named_workflow_and_returns_its_id():
    response = _response({"workflow_id": "wf-42"})

    with patch(
        "application_sdk.testing.e2e.workflows.kube_http_call",
        return_value=response,
    ) as call:
        workflow_id = await run_workflow(
            "conn", "handler", 8000, "extract", {"credential_guid": "abc"}
        )

    assert workflow_id == "wf-42"
    assert call.await_args is not None
    kwargs = call.await_args.kwargs
    assert kwargs["path"] == "/api/v1/workflows"
    assert kwargs["body"] == {"workflow_name": "extract", "credential_guid": "abc"}
    response.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_run_workflow_raises_on_a_non_2xx():
    response = _response({}, status=500)
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=MagicMock()
    )

    with (
        patch(
            "application_sdk.testing.e2e.workflows.kube_http_call",
            return_value=response,
        ),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await run_workflow("conn", "handler", 8000, "extract", {})


@pytest.mark.asyncio
async def test_one_tunnel_serves_the_whole_wait():
    """Not one ``kubectl`` process per probe — the rough edge FND-241 named."""
    session = _RecordingSession(
        _response({"status": "RUNNING"}),
        _response({"status": "RUNNING"}),
        _response({"status": "COMPLETED", "workflow_id": "wf-42"}),
    )
    opens: list[int] = []

    with patch(
        "application_sdk.testing.e2e.workflows.port_forward",
        lambda *args, **kwargs: _one_session(session, opens),
    ):
        result = await wait_for_workflow(
            "conn", "handler", 8000, "wf-42", timeout=5.0, poll_interval=0.0
        )

    assert result["status"] == "COMPLETED"
    assert opens == [1], "the tunnel was rebuilt mid-poll"
    assert session.calls == [("GET", "/api/v1/workflows/wf-42")] * 3


@pytest.mark.asyncio
async def test_a_workflow_that_never_settles_times_out_with_the_last_status():
    session = _RecordingSession(_response({"status": "RUNNING"}))
    opens: list[int] = []

    with (
        patch(
            "application_sdk.testing.e2e.workflows.port_forward",
            lambda *args, **kwargs: _one_session(session, opens),
        ),
        pytest.raises(TimeoutError, match="last status=running"),
    ):
        await wait_for_workflow(
            "conn", "handler", 8000, "wf-42", timeout=0.05, poll_interval=0.0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "FAILED", "Cancelled", "terminated"])
async def test_every_terminal_state_ends_the_wait(status: str):
    """Case-insensitively: the handler's casing has changed before now."""
    session = _RecordingSession(_response({"status": status}))
    opens: list[int] = []

    with patch(
        "application_sdk.testing.e2e.workflows.port_forward",
        lambda *args, **kwargs: _one_session(session, opens),
    ):
        result = await wait_for_workflow(
            "conn", "handler", 8000, "wf-42", timeout=5.0, poll_interval=0.0
        )

    assert result["status"] == status
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_a_status_read_that_fails_is_not_swallowed():
    """A 500 from the handler ends the wait; it is not polled through."""
    response = _response({}, status=500)
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=MagicMock()
    )
    session = _RecordingSession(response)
    opens: list[int] = []

    with (
        patch(
            "application_sdk.testing.e2e.workflows.port_forward",
            lambda *args, **kwargs: _one_session(session, opens),
        ),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await wait_for_workflow(
            "conn", "handler", 8000, "wf-42", timeout=5.0, poll_interval=0.0
        )
