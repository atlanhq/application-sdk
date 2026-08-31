"""Unit tests for the handler-service workflow helpers.

This module had no test file before FND-241. Two tests earn their keep:

* :func:`test_one_tunnel_serves_the_whole_wait` — the poll opened and tore down a
  ``kubectl port-forward`` on *every* probe, and nothing pinned that it no longer
  does. A regression there is invisible in a unit suite and expensive in a live
  run, and it is an easy one to reintroduce: the probe is the loop body, so a
  context manager moved inside it is a tunnel per iteration.
* :func:`test_a_handler_blip_is_polled_through` — the wait had no transient-error
  policy at all until FND-240, so one 502 from a handler pod mid-restart ended a
  five-minute wait on its first poll and ended it as a raw ``httpx`` error,
  reading as a test failure rather than as an unreadable dependency.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from application_sdk.testing.e2e.workflows import run_workflow, wait_for_workflow
from application_sdk.testing.harness._errors import WaitIndeterminateError


def _response(payload: dict[str, Any], status: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json = MagicMock(return_value=payload)
    response.raise_for_status = MagicMock()
    return response


class _RecordingSession:
    """A port-forward session answering a scripted sequence, repeating the last.

    A scripted item may be an ``Exception``, in which case ``request`` raises it.
    That matters because a dropped tunnel surfaces *from the request call*, not
    from the response object — a fake that can only fail at ``response.json()``
    forces a test to inject the failure one layer too late and then describe it
    as something it is not.
    """

    def __init__(self, *responses: MagicMock | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def request(
        self, method: str, path: str, *, body: Any = None, headers: Any = None
    ) -> MagicMock:
        self.calls.append((method, path))
        item = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


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


def _failing(status: int) -> MagicMock:
    """A handler response whose ``raise_for_status`` raises, as ``httpx`` does.

    The status is set on the *error's* response as well as the outer mock: the
    transient classifier reads ``error.response.status_code``, and a bare
    ``MagicMock`` there compares truthy against every threshold — which would
    make this test pass whatever the classifier decided.
    """
    response = _response({}, status=status)
    inner = MagicMock(spec=httpx.Response)
    inner.status_code = status
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=inner
    )
    return response


@pytest.mark.asyncio
async def test_a_handler_blip_is_polled_through():
    """The gap FND-240 closes: a 502 from a pod mid-restart used to end the wait
    on its first poll, because ``raise_for_status()`` sat bare in the loop."""
    session = _RecordingSession(
        _failing(502),
        _failing(502),
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
    assert len(session.calls) == 3


@pytest.mark.asyncio
async def test_an_unreadable_handler_is_indeterminate_not_a_timeout():
    """A pod that never answers is not evidence about the workflow.

    Reporting it as a timeout would say the workflow did not finish in time,
    which is a claim about the thing under test that a wait which never read it
    is not entitled to make.
    """
    session = _RecordingSession(_failing(503))
    opens: list[int] = []

    with (
        patch(
            "application_sdk.testing.e2e.workflows.port_forward",
            lambda *args, **kwargs: _one_session(session, opens),
        ),
        pytest.raises(WaitIndeterminateError, match="workflow wf-42"),
    ):
        await wait_for_workflow(
            "conn", "handler", 8000, "wf-42", timeout=60.0, poll_interval=0.0
        )

    # The streak, not the budget: five consecutive unreadable polls, well inside
    # a 60-second window that would otherwise have allowed hundreds.
    assert len(session.calls) == 5


@pytest.mark.asyncio
async def test_a_4xx_ends_the_wait_rather_than_being_polled_through():
    """A 404 on a workflow id is a wrong id, and no amount of waiting turns it
    into the right one. Absorbing it would spend the whole budget on a typo and
    then report a timeout."""
    session = _RecordingSession(_failing(404))
    opens: list[int] = []

    with (
        patch(
            "application_sdk.testing.e2e.workflows.port_forward",
            lambda *args, **kwargs: _one_session(session, opens),
        ),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await wait_for_workflow(
            "conn", "handler", 8000, "wf-42", timeout=60.0, poll_interval=0.0
        )

    assert len(session.calls) == 1, "a 4xx must not be retried"


@pytest.mark.asyncio
async def test_a_transport_error_from_the_request_is_absorbed():
    """The shape a dropped tunnel actually takes: ``session.request`` raises.

    This replaces a test named ``test_a_dropped_tunnel_is_a_blip_not_an_answer``
    whose body injected ``httpx.ReadError`` from ``response.json()`` — i.e. after
    the request had already succeeded — and whose docstring claimed
    ``PortForward.request`` rebuilds the tunnel and "the next poll gets a fresh
    tunnel". Neither was observable here: the fake session has no rebuild logic,
    and ``opens`` was never asserted. The name and the docstring described a
    mechanism the body never reached. Rebuild-on-transport-error is
    ``PortForward``'s own behaviour and is covered in
    ``tests/unit/testing/harness/cluster/test_portforward.py``; what this file can
    honestly assert is that the wait absorbs the error and carries on.
    """
    session = _RecordingSession(
        httpx.ReadError("tunnel died"),
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
    assert len(session.calls) == 2, "the failed request must have been retried"


@pytest.mark.asyncio
async def test_a_body_that_fails_to_decode_is_absorbed():
    """A truncated read surfacing from ``response.json()`` rather than from the
    request — a real second shape, and now named for what it is."""
    truncated = MagicMock(spec=httpx.Response)
    truncated.raise_for_status = MagicMock()
    truncated.json = MagicMock(side_effect=httpx.ReadError("truncated"))
    session = _RecordingSession(
        truncated, _response({"status": "COMPLETED", "workflow_id": "wf-42"})
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
    assert len(session.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"status": None}, {"status": 7}])
async def test_a_status_field_that_is_not_a_string_keeps_the_wait_alive(
    payload: dict[str, Any],
):
    """The predicate deciding whether to keep waiting must not be the thing that
    crashes the wait. A missing or non-string ``status`` reads as "not terminal",
    and the timeout message says ``unknown``."""
    session = _RecordingSession(_response(payload))
    opens: list[int] = []

    with (
        patch(
            "application_sdk.testing.e2e.workflows.port_forward",
            lambda *args, **kwargs: _one_session(session, opens),
        ),
        pytest.raises(TimeoutError, match="last status=unknown"),
    ):
        await wait_for_workflow(
            "conn", "handler", 8000, "wf-42", timeout=0.05, poll_interval=0.0
        )
