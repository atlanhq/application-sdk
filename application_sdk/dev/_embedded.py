"""In-process workflow runtime for local app development."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddedRuntime:
    """Connection details for the in-process workflow runtime."""

    host: str
    """``host:port`` the SDK should connect to (loopback, random free port)."""

    namespace: str = "default"
    """Workflow namespace served by the embedded runtime."""


@asynccontextmanager
async def embedded_runtime(
    *,
    namespace: str = "default",
    log_level: str = "warn",
) -> AsyncIterator[EmbeddedRuntime]:
    """Boot an in-process workflow runtime for local development.

    On first invocation the runtime binary (~30 MB) is downloaded to
    ``~/.cache`` and cached. Subsequent calls are instant. The runtime is
    torn down when the context exits.
    """
    from temporalio.testing import WorkflowEnvironment  # noqa: PLC0415

    logger.info("Starting embedded workflow runtime")
    env = await WorkflowEnvironment.start_local(
        namespace=namespace,
        dev_server_log_level=log_level,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "frontend.WorkerHeartbeatsEnabled=true",
        ],
    )
    try:
        host = env.client.service_client.config.target_host
        logger.info("Embedded workflow runtime ready at %s", host)
        yield EmbeddedRuntime(host=host, namespace=namespace)
    finally:
        logger.info("Shutting down embedded workflow runtime")
        await env.shutdown()
