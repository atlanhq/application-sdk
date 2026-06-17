"""In-process workflow runtime for local app development."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Stable download location for the Temporal CLI dev-server binary. Mirrors the
# Dapr helper's ``~/.cache/atlan-sdk`` convention so the binary is fetched once
# and reused across runs (and is cacheable in CI). ``start_local`` defaults to
# the system temp dir, which fresh CI runners don't persist.
_CACHE_DIR = Path.home() / ".cache" / "atlan-sdk" / "temporal"


@dataclass(frozen=True)
class EmbeddedRuntime:
    """Connection details for the in-process workflow runtime."""

    host: str
    """``host:port`` the SDK should connect to (loopback, random free port)."""

    namespace: str = "default"
    """Workflow namespace served by the embedded runtime."""

    ui_url: str | None = None
    """Temporal Web UI URL when enabled for local development."""


@asynccontextmanager
async def embedded_runtime(
    *,
    namespace: str = "default",
    log_level: str = "warn",
    temporal_ui: bool = False,
    temporal_ui_port: int = 8233,
) -> AsyncIterator[EmbeddedRuntime]:
    """Boot an in-process workflow runtime for local development.

    On first invocation the runtime binary (~30 MB) is downloaded to
    ``~/.cache`` and cached. Subsequent calls are instant. The runtime is
    torn down when the context exits.
    """
    from temporalio.testing import WorkflowEnvironment  # noqa: PLC0415

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Starting embedded workflow runtime")
    env = await WorkflowEnvironment.start_local(
        namespace=namespace,
        download_dest_dir=str(_CACHE_DIR),
        ui=temporal_ui,
        ui_port=temporal_ui_port if temporal_ui else None,
        dev_server_log_level=log_level,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "frontend.WorkerHeartbeatsEnabled=true",
        ],
    )
    try:
        host = env.client.service_client.config.target_host
        logger.info("Embedded workflow runtime ready at %s", host)
        ui_url = f"http://127.0.0.1:{temporal_ui_port}" if temporal_ui else None
        yield EmbeddedRuntime(host=host, namespace=namespace, ui_url=ui_url)
    finally:
        logger.info("Shutting down embedded workflow runtime")
        await env.shutdown()
