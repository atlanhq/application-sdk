"""Health check HTTP server for Temporal workers.

Workers run separately from handlers and need their own health endpoints
for Kubernetes liveness and readiness probes. This module provides a
lightweight HTTP server (using asyncio) that runs alongside the worker.

Endpoints:
    GET /health - Always returns 200 (container is running)
    GET /ready  - Returns 200 only if Temporal client is connected
    GET /live   - Returns 200 if worker is responsive

Usage:
    from application_sdk.server.health import WorkerHealthServer

    # Start health server alongside worker
    async def run():
        health_server = WorkerHealthServer(port=8081)

        async with health_server:
            # Server is running on port 8081
            await worker.run()

    # Or manually start/stop
    health_server = WorkerHealthServer()
    await health_server.start()
    # ... run worker ...
    await health_server.stop()
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, Protocol

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger("health")


def _utc_now() -> datetime:
    """Return current UTC time (timezone-aware)."""
    return datetime.now(UTC)


class TemporalClientProtocol(Protocol):
    """Protocol for Temporal client health checks.

    Defines the minimum interface needed to verify the client was initialised.
    """

    @property
    def identity(self) -> str:
        """Get the client identity."""
        ...


@dataclass
class HealthStatus:
    """Health check status response.

    Attributes:
        healthy: Whether the check passed.
        message: Human-readable status message.
        details: Additional details for debugging.
        checked_at: When the check was performed.
    """

    healthy: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "healthy": self.healthy,
            "message": self.message,
            "details": self.details,
            "checked_at": self.checked_at.isoformat(),
        }


class WorkerHealthServer:
    """Lightweight health check HTTP server for workers.

    Provides health, readiness, and liveness endpoints for Kubernetes probes.
    Uses asyncio.Server directly to minimize dependencies (no FastAPI/aiohttp).

    The server can track Temporal client connectivity and worker activity
    for more sophisticated health checks.

    Attributes:
        port: Port to listen on (default 8081).
        host: Host to bind to (default 0.0.0.0).

    Example:
        # As context manager
        async with WorkerHealthServer(port=8081) as server:
            server.set_temporal_client(client)
            await worker.run()

        # Manual control
        server = WorkerHealthServer()
        await server.start()
        server.set_temporal_client(client)
        # ... run worker ...
        await server.stop()
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8081,
    ) -> None:
        """Initialize the health server.

        Args:
            host: Host to bind to.
            port: Port to listen on.
        """
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._temporal_client: TemporalClientProtocol | None = None
        self._started_at: datetime | None = None
        self._last_activity: datetime | None = None
        self._request_count: int = 0

    def set_temporal_client(self, client: TemporalClientProtocol) -> None:
        """Set the Temporal client for readiness checks.

        Args:
            client: Temporal client to use for connectivity checks.
        """
        self._temporal_client = client

    def record_activity(self) -> None:
        """Record that the worker performed activity.

        Call this when the worker processes a task to update liveness tracking.
        """
        self._last_activity = _utc_now()

    async def check_health(self) -> HealthStatus:
        """Basic health check - is the process running?

        Always returns healthy if the server is running.
        """
        return HealthStatus(
            healthy=True,
            message="Worker process is running",
            details={
                "started_at": self._started_at.isoformat()
                if self._started_at
                else None,
                "request_count": self._request_count,
            },
        )

    async def check_ready(self) -> HealthStatus:
        """Readiness check - has the worker successfully initialised?

        Returns healthy once the Temporal client has been configured, which
        happens after a successful connection to Temporal at startup.

        Workers do not serve HTTP traffic, so Kubernetes readiness routing does
        not apply to them. The sole purpose of this probe is to delay task
        dispatch until the worker process is fully up. Making a live Temporal
        gRPC call here is wrong: it fails under event-loop load and causes the
        probe to return 503, which Kubernetes treats as a pod kill trigger.
        """
        if self._temporal_client is None:
            return HealthStatus(
                healthy=False,
                message="Temporal client not configured",
            )

        return HealthStatus(
            healthy=True,
            message="Worker is ready",
            details={"identity": self._temporal_client.identity},
        )

    async def check_live(self) -> HealthStatus:
        """Liveness check - is the worker responsive?

        Returns healthy if the worker is not stuck. Kubernetes uses this
        to determine if the pod should be restarted.

        Currently always returns healthy if the process is running.
        Could be extended to check for stuck workers (no activity for too long).
        """
        return HealthStatus(
            healthy=True,
            message="Worker is responsive",
            details={
                "last_activity": self._last_activity.isoformat()
                if self._last_activity
                else None,
            },
        )

    async def _handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming HTTP request.

        Parses the request path and returns the appropriate health status.
        """
        self._request_count += 1

        try:
            # Read request line
            request_line = await asyncio.wait_for(
                reader.readline(),
                timeout=5.0,
            )
            request_text = request_line.decode("utf-8").strip()

            # Parse method and path
            parts = request_text.split(" ")
            if len(parts) < 2:
                await self._send_response(
                    writer, HTTPStatus.BAD_REQUEST, {"error": "Invalid request"}
                )
                return

            method, path = parts[0], parts[1]

            # Only accept GET requests
            if method != "GET":
                await self._send_response(
                    writer,
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "Method not allowed"},
                )
                return

            # Route to appropriate handler
            if path == "/health":
                status = await self.check_health()
            elif path == "/ready":
                status = await self.check_ready()
            elif path == "/live":
                status = await self.check_live()
            else:
                await self._send_response(
                    writer, HTTPStatus.NOT_FOUND, {"error": "Not found"}
                )
                return

            # Send response
            http_status = (
                HTTPStatus.OK if status.healthy else HTTPStatus.SERVICE_UNAVAILABLE
            )
            await self._send_response(writer, http_status, status.to_dict())

        except TimeoutError:
            logger.debug("Health check request timed out")
        except Exception as e:
            logger.error("Error handling health check request", error=str(e))
            with contextlib.suppress(Exception):
                await self._send_response(
                    writer, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)}
                )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status: HTTPStatus,
        body: dict[str, Any],
    ) -> None:
        """Send an HTTP response.

        Args:
            writer: The stream writer to send to.
            status: HTTP status code.
            body: JSON body to send.
        """
        import json

        body_bytes = json.dumps(body).encode("utf-8")
        response = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(response.encode("utf-8"))
        writer.write(body_bytes)
        await writer.drain()

    async def start(self) -> None:
        """Start the health check server.

        Raises:
            OSError: If the port is already in use.
        """
        if self._server is not None:
            logger.warning("Health server already running")
            return

        self._server = await asyncio.start_server(
            self._handle_request,
            self.host,
            self.port,
        )
        self._started_at = _utc_now()

        logger.info(
            "Health server started",
            host=self.host,
            port=self.port,
        )

    async def stop(self) -> None:
        """Stop the health check server."""
        if self._server is None:
            return

        self._server.close()
        await self._server.wait_closed()
        self._server = None

        logger.info("Health server stopped")

    async def __aenter__(self) -> "WorkerHealthServer":
        """Start server as context manager."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Stop server when exiting context."""
        await self.stop()


async def run_health_server(
    *,
    host: str = "0.0.0.0",
    port: int = 8081,
    temporal_client: TemporalClientProtocol | None = None,
) -> WorkerHealthServer:
    """Create and start a health server.

    Convenience function that creates a server, optionally sets the
    Temporal client, and starts it.

    Args:
        host: Host to bind to.
        port: Port to listen on.
        temporal_client: Optional Temporal client for readiness checks.

    Returns:
        Running WorkerHealthServer instance.

    Example:
        server = await run_health_server(
            port=8081,
            temporal_client=client,
        )
        # ... run worker ...
        await server.stop()
    """
    server = WorkerHealthServer(host=host, port=port)

    if temporal_client:
        server.set_temporal_client(temporal_client)

    await server.start()
    return server
