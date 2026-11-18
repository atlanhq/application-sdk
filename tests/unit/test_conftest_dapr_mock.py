"""The unit tree's Dapr sidecar interceptor intercepts, and fails loudly when stale.

The fixture it replaced patched ``_dapr.client.DaprClient`` after that symbol
was gone, and a helper swallowed the ``AttributeError``, so every unit test ran
it for nothing (FND-4287). These tests pin both halves: an unmocked sidecar
call is refused, and a target the interceptor cannot find raises.
"""

from __future__ import annotations

from types import ModuleType

import httpx
import pytest

from application_sdk.infrastructure._dapr import http as dapr_http
from application_sdk.infrastructure._dapr.http import (
    AsyncDaprClient,
    get_dapr_component_types,
    wait_for_dapr_sidecar,
)
from tests.unit.conftest import UnmockedDaprSidecarCallError, intercept_dapr_sidecar


async def test_unmocked_client_call_is_refused() -> None:
    async with AsyncDaprClient(base_url="http://localhost:3500") as client:
        with pytest.raises(UnmockedDaprSidecarCallError, match="/v1.0/state/"):
            await client.get_state("statestore", "key")


async def test_refusal_is_not_retried_as_a_transport_error() -> None:
    """The client's RetryTransport retries httpx errors over ~15s; a refusal
    that looked like one would stall the test instead of failing it."""
    client = AsyncDaprClient(base_url="http://localhost:3500", backoff_factor=0)
    with pytest.raises(UnmockedDaprSidecarCallError):
        await client.get_secret("secretstore", "key")
    await client.close()


async def test_client_still_builds_real_transports() -> None:
    """Pool-limit and retry tests inspect these objects; the interceptor
    must change only what sending does."""
    client = AsyncDaprClient(base_url="http://localhost:3500")
    inner = client._client._transport._async_transport  # type: ignore[attr-defined]  # — RetryTransport's wrapped transport
    assert isinstance(inner, httpx.AsyncHTTPTransport)
    await client.close()


async def test_sidecar_poll_client_is_intercepted(monkeypatch, loguru_capture) -> None:
    """``wait_for_dapr_sidecar`` builds its own ``httpx.AsyncClient`` and turns
    every poll failure into a timeout warning; the warning must carry the
    refusal, not a connection error from a real sidecar port."""
    monkeypatch.setattr(dapr_http, "DEPLOYMENT_NAME", "deployed")
    await wait_for_dapr_sidecar(timeout=0.05, interval=0.01)
    refused = [
        r
        for r in loguru_capture
        if r["exception"] is not None
        and r["exception"].type is UnmockedDaprSidecarCallError
    ]
    assert refused, [r["message"] for r in loguru_capture]
    assert "/v1.0/healthz" in str(refused[-1]["exception"].value)


async def test_best_effort_metadata_read_degrades_to_empty() -> None:
    """Same outcome as a POSIX leg's blocked socket: the caller's own
    ``except Exception`` turns the refusal into ``{}``."""
    assert await get_dapr_component_types() == {}


def test_interceptor_leaves_real_httpx_alone() -> None:
    assert dapr_http.httpx is not httpx
    assert httpx.AsyncHTTPTransport.handle_async_request is not (
        dapr_http.httpx.AsyncHTTPTransport.handle_async_request
    )


def test_interceptor_raises_on_a_module_without_httpx() -> None:
    stale = ModuleType("example_moved_module")
    with pytest.raises(AttributeError, match="no 'httpx' global"):
        with intercept_dapr_sidecar(stale):
            pass


def test_secret_store_mock_target_exists() -> None:
    """``mock_secret_store`` uses a plain ``patch``; prove its target is the
    mock, so a rename errors at setup rather than running unmocked."""
    from application_sdk.infrastructure import secrets

    assert secrets.get_deployment_secret.__class__.__module__ == "unittest.mock"
