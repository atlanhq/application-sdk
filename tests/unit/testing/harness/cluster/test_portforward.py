"""Unit tests for the ``kubectl port-forward`` transport.

Moved here from ``tests/unit/testing/e2e/test_portforward.py`` with the code
(FND-241). The four original tests are unchanged apart from their patch targets;
everything below the "one tunnel per session" divider is new behaviour.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from application_sdk.testing.harness.cluster._portforward import (
    PortForward,
    _wait_for_port,
    kube_http_call,
    port_forward,
)

# test_find_free_port_returns_integer lives in
# tests/integration/testing/test_portforward.py (_find_free_port binds a real
# socket; not permitted in the hermetic unit suite)

_MODULE = "application_sdk.testing.harness.cluster._portforward"
_STUB_PORT = f"{_MODULE}._find_free_port"
_STUB_WAIT = f"{_MODULE}._wait_for_port"


def _kubectl_proc() -> MagicMock:
    proc = MagicMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


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
    pf_proc = _kubectl_proc()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc) as mock_exec,
        patch(_STUB_WAIT, new=AsyncMock()),
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
    pf_proc = _kubectl_proc()
    mock_response = MagicMock(spec=httpx.Response)

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc),
        patch(_STUB_WAIT, new=AsyncMock()),
        patch("httpx.AsyncClient.request", new=AsyncMock(return_value=mock_response)),
    ):
        await kube_http_call("ns", "svc", 8080, "GET", "/")

    pf_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_kube_http_call_terminates_port_forward_on_error():
    """Port-forward process is terminated even when the HTTP request raises."""
    pf_proc = _kubectl_proc()

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc),
        patch(_STUB_WAIT, new=AsyncMock()),
        patch(
            "httpx.AsyncClient.request",
            new=AsyncMock(side_effect=httpx.ConnectError("refused")),
        ),
        pytest.raises(httpx.ConnectError),
    ):
        await kube_http_call("ns", "svc", 8080, "GET", "/")

    # Two attempts (the rebuild), so two tunnels, so two terminations
    assert pf_proc.terminate.call_count == 2


# ---------------------------------------------------------------------------
# One tunnel per session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_tunnel_serves_a_whole_session():
    """The property the poll loop needed: not one ``kubectl`` process per probe.

    ``wait_for_workflow`` opened and tore down a tunnel on every poll — up to 60
    inside a single 300-second wait, to read one status string.
    """
    pf_proc = _kubectl_proc()
    mock_response = MagicMock(spec=httpx.Response)

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc) as mock_exec,
        patch(_STUB_WAIT, new=AsyncMock()),
        patch("httpx.AsyncClient.request", new=AsyncMock(return_value=mock_response)),
    ):
        async with port_forward("ns", "svc", 8080) as session:
            for _ in range(4):
                await session.request("GET", "/status")

    assert mock_exec.call_count == 1
    pf_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_a_session_that_makes_no_call_opens_no_tunnel():
    """The tunnel opens on first use, so an unused session costs nothing."""
    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec") as mock_exec,
        patch(_STUB_WAIT, new=AsyncMock()),
    ):
        async with port_forward("ns", "svc", 8080):
            pass

    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_a_transport_error_rebuilds_the_tunnel_and_re_attempts():
    """A half-dead tunnel is exactly what a transport retry exists to escape.

    Same trade the AE client's pool makes: pooled on the happy path, a fresh
    connection on the retry.
    """
    procs = [_kubectl_proc(), _kubectl_proc()]
    mock_response = MagicMock(spec=httpx.Response)

    with (
        patch(_STUB_PORT, side_effect=[54321, 54322]),
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
        patch(_STUB_WAIT, new=AsyncMock()),
        patch(
            "httpx.AsyncClient.request",
            new=AsyncMock(
                side_effect=[httpx.ConnectError("tunnel died"), mock_response]
            ),
        ),
    ):
        async with port_forward("ns", "svc", 8080) as session:
            result = await session.request("GET", "/status")

    assert result is mock_response
    assert mock_exec.call_count == 2
    # The dead tunnel is torn down before the new one opens
    procs[0].terminate.assert_called_once()
    procs[1].terminate.assert_called_once()


@pytest.mark.asyncio
async def test_two_consecutive_transport_errors_propagate():
    """Rebuilt and still failing is a Service that is genuinely unreachable."""
    with (
        patch(_STUB_PORT, return_value=54321),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=lambda *a, **k: _kubectl_proc(),
        ),
        patch(_STUB_WAIT, new=AsyncMock()),
        patch(
            "httpx.AsyncClient.request",
            new=AsyncMock(side_effect=httpx.ConnectError("still down")),
        ),
        pytest.raises(httpx.ConnectError),
    ):
        async with port_forward("ns", "svc", 8080) as session:
            await session.request("GET", "/status")


@pytest.mark.asyncio
async def test_a_non_transport_failure_is_not_retried():
    """A 500 comes back as a response; a bug in the call is not a dead tunnel."""
    pf_proc = _kubectl_proc()

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc) as mock_exec,
        patch(_STUB_WAIT, new=AsyncMock()),
        patch(
            "httpx.AsyncClient.request",
            new=AsyncMock(side_effect=ValueError("bad body")),
        ),
        pytest.raises(ValueError),
    ):
        async with port_forward("ns", "svc", 8080) as session:
            await session.request("POST", "/api/v1/workflows", body={"a": 1})

    assert mock_exec.call_count == 1


@pytest.mark.asyncio
async def test_a_tunnel_that_never_opens_does_not_leak_the_process():
    """The readiness wait failing still leaves a running ``kubectl`` behind."""
    pf_proc = _kubectl_proc()

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc),
        patch(_STUB_WAIT, new=AsyncMock(side_effect=TimeoutError("never opened"))),
        pytest.raises(TimeoutError),
    ):
        async with port_forward("ns", "svc", 8080) as session:
            await session.request("GET", "/status")

    pf_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_a_kubectl_that_ignores_terminate_is_killed():
    pf_proc = _kubectl_proc()
    pf_proc.wait = AsyncMock(side_effect=TimeoutError)
    mock_response = MagicMock(spec=httpx.Response)

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc),
        patch(_STUB_WAIT, new=AsyncMock()),
        patch("httpx.AsyncClient.request", new=AsyncMock(return_value=mock_response)),
    ):
        async with port_forward("ns", "svc", 8080) as session:
            await session.request("GET", "/status")

    pf_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_closing_a_session_twice_is_harmless():
    """``aclose`` is idempotent, so the context manager's exit cannot double-free."""
    async with port_forward("ns", "svc", 8080) as session:
        await session.aclose()
        await session.aclose()


@pytest.mark.asyncio
async def test_the_tunnel_pins_the_kube_context_it_was_given():
    """Reads from one cluster and a tunnel into another is a silent wrong answer.

    Both calls succeed — `kubectl` is perfectly happy with the current context —
    so nothing errors and nothing is logged. The only symptom is a result that
    makes no sense, which is why this is asserted on the argv.
    """
    pf_proc = _kubectl_proc()

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc) as mock_exec,
        patch(_STUB_WAIT, new=AsyncMock()),
        patch("httpx.AsyncClient.request", new=AsyncMock()),
    ):
        async with port_forward("ns", "svc", 8080, kube_context="e2e-gcp") as session:
            await session.request("GET", "/status")

    argv = list(mock_exec.call_args[0])
    assert "--context" in argv
    assert argv[argv.index("--context") + 1] == "e2e-gcp"


@pytest.mark.asyncio
async def test_no_context_named_means_no_context_flag():
    """`None` is "whatever is current", which is right when nothing named one."""
    pf_proc = _kubectl_proc()

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc) as mock_exec,
        patch(_STUB_WAIT, new=AsyncMock()),
        patch("httpx.AsyncClient.request", new=AsyncMock()),
    ):
        async with port_forward("ns", "svc", 8080) as session:
            await session.request("GET", "/status")

    assert "--context" not in list(mock_exec.call_args[0])


@pytest.mark.asyncio
async def test_concurrent_requests_open_exactly_one_tunnel():
    """The lazy open is check-then-act, so two callers could each spawn a kubectl.

    Only one would be recorded in `_proc`; the other would run unreferenced for
    the rest of the session. Sequential use never hits it, which is exactly why a
    leaked subprocess is not something to leave to usage.
    """
    procs: list[MagicMock] = []

    async def _spawn(*_args: object, **_kwargs: object) -> MagicMock:
        # Yield control mid-open, so a second caller can interleave here — which
        # is the whole race, and would not happen with a synchronous double.
        await asyncio.sleep(0)
        proc = _kubectl_proc()
        procs.append(proc)
        return proc

    with (
        patch(_STUB_PORT, side_effect=[54321, 54322, 54323, 54324]),
        patch("asyncio.create_subprocess_exec", new=_spawn),
        patch(_STUB_WAIT, new=AsyncMock()),
        patch("httpx.AsyncClient.request", new=AsyncMock()),
    ):
        async with port_forward("ns", "svc", 8080) as session:
            await asyncio.gather(*(session.request("GET", "/status") for _ in range(4)))

    assert (
        len(procs) == 1
    ), f"{len(procs)} kubectl processes spawned, {len(procs) - 1} leaked"


@pytest.mark.asyncio
async def test_the_e2e_module_re_exports_the_same_objects():
    """The compatibility guarantee of the move: same function, same class."""
    from application_sdk.testing import e2e
    from application_sdk.testing.e2e import portforward as shim
    from application_sdk.testing.harness.cluster import _portforward as moved

    assert shim.kube_http_call is moved.kube_http_call
    assert shim.port_forward is moved.port_forward
    assert shim.PortForward is moved.PortForward
    assert e2e.kube_http_call is moved.kube_http_call


@pytest.mark.asyncio
async def test_a_request_timeout_override_reaches_the_client():
    pf_proc = _kubectl_proc()
    request = AsyncMock(return_value=MagicMock(spec=httpx.Response))

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc),
        patch(_STUB_WAIT, new=AsyncMock()),
        patch("httpx.AsyncClient.request", new=request),
    ):
        async with port_forward("ns", "svc", 8080) as session:
            await session.request("GET", "/status", timeout=2.5)

    assert request.await_args is not None
    kwargs: dict[str, Any] = request.await_args.kwargs
    assert kwargs["timeout"] == 2.5


@pytest.mark.asyncio
async def test_no_override_leaves_the_sessions_own_timeout_in_place():
    """``httpx`` reads an explicit ``timeout=None`` as "no timeout at all".

    So the default has to be *omitted*, not forwarded — forwarding it would
    silently remove the bound the session was configured with.
    """
    pf_proc = _kubectl_proc()
    request = AsyncMock(return_value=MagicMock(spec=httpx.Response))

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc),
        patch(_STUB_WAIT, new=AsyncMock()),
        patch("httpx.AsyncClient.request", new=request),
    ):
        async with port_forward("ns", "svc", 8080, timeout=11.0) as session:
            await session.request("GET", "/status")

    assert request.await_args is not None
    assert "timeout" not in request.await_args.kwargs
    assert session.timeout == 11.0


# ---------------------------------------------------------------------------
# The tunnel's address, for a client this module cannot make the call for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_address_opens_the_tunnel_and_names_its_near_end():
    """``temporalio`` speaks gRPC and takes a bare ``host:port``, so the Temporal
    reader needs the tunnel's near end rather than an HTTP session over it."""
    pf_proc = _kubectl_proc()

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc) as mock_exec,
        patch(_STUB_WAIT, new=AsyncMock()),
    ):
        async with port_forward("ns", "svc", 7233) as session:
            assert await session.address() == "127.0.0.1:54321"

    assert mock_exec.call_count == 1
    pf_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_the_address_and_a_request_share_one_tunnel():
    """The reason ``address`` is on this class rather than a second
    implementation elsewhere: a session that is asked for both opens one
    ``kubectl`` process, not two."""
    pf_proc = _kubectl_proc()
    mock_response = MagicMock(spec=httpx.Response)

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc) as mock_exec,
        patch(_STUB_WAIT, new=AsyncMock()),
        patch("httpx.AsyncClient.request", new=AsyncMock(return_value=mock_response)),
    ):
        async with port_forward("ns", "svc", 7233) as session:
            address = await session.address()
            await session.request("GET", "/status")
            assert await session.address() == address

    assert mock_exec.call_count == 1


@pytest.mark.asyncio
async def test_a_closed_session_reopens_on_the_next_address_call():
    """``aclose`` clears the port as well as the client, so a reused session does
    not hand out the address of a tunnel it has already terminated."""
    procs = [_kubectl_proc(), _kubectl_proc()]

    with (
        patch(_STUB_PORT, side_effect=[54321, 54322]),
        patch("asyncio.create_subprocess_exec", side_effect=procs),
        patch(_STUB_WAIT, new=AsyncMock()),
    ):
        async with port_forward("ns", "svc", 7233) as session:
            assert await session.address() == "127.0.0.1:54321"
            await session.aclose()
            assert await session.address() == "127.0.0.1:54322"


@pytest.mark.asyncio
async def test_an_address_on_a_tunnel_that_never_opens_does_not_leak_the_process():
    """Same leak the HTTP path guards: the ``kubectl`` child is already running by
    the time the readiness wait gives up."""
    pf_proc = _kubectl_proc()

    with (
        patch(_STUB_PORT, return_value=54321),
        patch("asyncio.create_subprocess_exec", return_value=pf_proc),
        patch(_STUB_WAIT, new=AsyncMock(side_effect=TimeoutError("never ready"))),
        pytest.raises(TimeoutError),
    ):
        async with port_forward("ns", "svc", 7233) as session:
            await session.address()

    pf_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_concurrent_first_calls_spawn_exactly_one_tunnel():
    """Both lazy-open entry points serialise on the same lock.

    `address()` and `request()` are two separate doors onto one spawn, so a guard
    covering only the HTTP one lets them race into two `kubectl` processes and
    leak the unreferenced one — the leak FND-241 closed, reopened by FND-247
    adding the second door.

    `_wait_for_port` is replaced with a function that actually yields, and that
    is the whole test. Patched with a bare `AsyncMock` it completes without ever
    returning to the event loop, so `gather` runs each task start-to-finish and
    the interleaving this exists to catch cannot occur — the test passes with the
    lock removed. Verified by removing it: one real suspension point is the
    difference between a regression test and a green line.
    """
    procs = [_kubectl_proc(), _kubectl_proc(), _kubectl_proc(), _kubectl_proc()]
    mock_response = MagicMock(spec=httpx.Response)

    async def _yielding_wait(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(0)

    with (
        patch(_STUB_PORT, side_effect=[54321, 54322, 54323, 54324]),
        patch("asyncio.create_subprocess_exec", side_effect=procs) as mock_exec,
        patch(_STUB_WAIT, new=_yielding_wait),
        patch("httpx.AsyncClient.request", new=AsyncMock(return_value=mock_response)),
    ):
        async with port_forward("ns", "svc", 7233) as session:
            await asyncio.gather(
                session.address(),
                session.request("GET", "/status"),
                session.address(),
                session.request("GET", "/status"),
            )

    assert mock_exec.call_count == 1, (
        f"{mock_exec.call_count} kubectl processes spawned for one tunnel; "
        "all but one are leaked"
    )


@pytest.mark.asyncio
async def test_aclose_clears_both_fields_so_the_next_open_rebuilds():
    """`aclose` has to clear the port as well as the client.

    Deliberately NOT a claim that the spawn/client window is closed — this is
    sequential, and a sequential test cannot demonstrate a race. What it pins is
    the invariant the window fix relies on: after `aclose`, neither `_client` nor
    `_local_port` survives, so nothing can point a fresh client at a terminated
    process by reading a stale port.
    """
    procs = [_kubectl_proc(), _kubectl_proc()]

    with (
        patch(_STUB_PORT, side_effect=[54321, 54322]),
        patch("asyncio.create_subprocess_exec", side_effect=procs),
        patch(_STUB_WAIT, new=AsyncMock()),
    ):
        session = PortForward("ns", "svc", 7233)
        client = await session._opened()
        assert str(client.base_url) == "http://127.0.0.1:54321"
        assert session._local_port == 54321

        await session.aclose()
        assert session._client is None
        assert session._local_port is None

        rebuilt = await session._opened()
        assert str(rebuilt.base_url) == "http://127.0.0.1:54322"
        assert session._local_port == 54322
        await session.aclose()
