"""Unit tests for kube_http_call port-forward helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from application_sdk.testing.e2e.portforward import (
    _find_free_port,
    _wait_for_port,
    kube_http_call,
)


def test_find_free_port_returns_integer():
    port = _find_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


@pytest.mark.asyncio
async def test_wait_for_port_success():
    """_wait_for_port resolves immediately when port is open."""
    reader = AsyncMock()
    writer = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()

    with patch("asyncio.open_connection", return_value=(reader, writer)):
        # Should not raise
        await _wait_for_port(9999, timeout=1.0)


@pytest.mark.asyncio
async def test_wait_for_port_timeout():
    """_wait_for_port raises TimeoutError when port never opens."""
    with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
        with pytest.raises(TimeoutError):
            await _wait_for_port(9999, timeout=0.2)


@pytest.mark.asyncio
async def test_kube_http_call_starts_port_forward():
    """kube_http_call starts kubectl port-forward and makes the HTTP request."""
    pf_proc = MagicMock()
    pf_proc.terminate = MagicMock()
    pf_proc.kill = MagicMock()
    pf_proc.wait = AsyncMock()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200

    with (
        patch("asyncio.create_subprocess_exec", return_value=pf_proc) as mock_exec,
        patch(
            "application_sdk.testing.e2e.portforward._wait_for_port",
            new=AsyncMock(),
        ),
        patch(
            "httpx.AsyncClient.request",
            new=AsyncMock(return_value=mock_response),
        ),
    ):
        result = await kube_http_call(
            namespace="test-ns",
            service="test-svc",
            port=8080,
            method="GET",
            path="/health",
        )

    assert result is mock_response
    exec_args = mock_exec.call_args[0]
    assert exec_args[0] == "kubectl"
    assert "port-forward" in exec_args
    assert "svc/test-svc" in exec_args
    assert "-n" in exec_args
    ns_idx = list(exec_args).index("-n")
    assert exec_args[ns_idx + 1] == "test-ns"


@pytest.mark.asyncio
async def test_kube_http_call_terminates_port_forward_on_success():
    """Port-forward process is terminated even after a successful request."""
    pf_proc = MagicMock()
    pf_proc.terminate = MagicMock()
    pf_proc.kill = MagicMock()
    pf_proc.wait = AsyncMock()

    mock_response = MagicMock(spec=httpx.Response)

    with (
        patch("asyncio.create_subprocess_exec", return_value=pf_proc),
        patch(
            "application_sdk.testing.e2e.portforward._wait_for_port", new=AsyncMock()
        ),
        patch("httpx.AsyncClient.request", new=AsyncMock(return_value=mock_response)),
    ):
        await kube_http_call("ns", "svc", 8080, "GET", "/")

    pf_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_kube_http_call_terminates_port_forward_on_error():
    """Port-forward process is terminated even when the HTTP request raises."""
    pf_proc = MagicMock()
    pf_proc.terminate = MagicMock()
    pf_proc.kill = MagicMock()
    pf_proc.wait = AsyncMock()

    with (
        patch("asyncio.create_subprocess_exec", return_value=pf_proc),
        patch(
            "application_sdk.testing.e2e.portforward._wait_for_port", new=AsyncMock()
        ),
        patch(
            "httpx.AsyncClient.request",
            new=AsyncMock(side_effect=httpx.ConnectError("refused")),
        ),
    ):
        with pytest.raises(httpx.ConnectError):
            await kube_http_call("ns", "svc", 8080, "GET", "/")

    pf_proc.terminate.assert_called_once()
