"""Ephemeral kubectl port-forward helper for K8s e2e tests."""

import asyncio
import socket
from typing import Any

import httpx

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def _find_free_port() -> int:
    """Bind to port 0 to let the OS pick a free port, return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


async def _wait_for_port(
    port: int, host: str = "127.0.0.1", timeout: float = 10.0
) -> None:
    """Poll until TCP port accepts connections or timeout."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.0
            )
            writer.close()
            await writer.wait_closed()
            return
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.1)
    raise TimeoutError(f"Port {port} did not become ready within {timeout}s")


async def kube_http_call(
    namespace: str,
    service: str,
    port: int,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """Make an HTTP call to a K8s service via an ephemeral port-forward.

    Opens a ``kubectl port-forward`` tunnel for the duration of the request,
    makes the HTTP call, then closes the tunnel. This avoids idle TCP timeout
    issues that occur with persistent port-forwards.

    Args:
        namespace: K8s namespace where the service lives.
        service: Service name (without ``svc/`` prefix).
        port: Remote port to forward to.
        method: HTTP method (``GET``, ``POST``, etc.).
        path: Request path, e.g. ``"/health"``.
        body: Optional JSON body for POST/PUT requests.
        timeout: Total timeout in seconds for port-forward + HTTP request.

    Returns:
        The :class:`httpx.Response`.
    """
    local_port = _find_free_port()
    pf_proc = await asyncio.create_subprocess_exec(
        "kubectl",
        "port-forward",
        f"svc/{service}",
        f"{local_port}:{port}",
        "-n",
        namespace,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await _wait_for_port(local_port, timeout=min(10.0, timeout))
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{local_port}", timeout=timeout
        ) as client:
            response = await client.request(method, path, json=body)
        return response
    finally:
        pf_proc.terminate()
        try:
            await asyncio.wait_for(pf_proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pf_proc.kill()
