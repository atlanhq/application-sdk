#!/usr/bin/env python3
"""
Measure memory usage of the FastAPI API server when APPLICATION_MODE=SERVER.

Run with:
  APPLICATION_MODE=SERVER uv run python scripts/measure_server_memory.py

Requires the workflow client to connect (e.g. Temporal running via `uv run poe start-deps`
or WORKFLOW_HOST/WORKFLOW_PORT pointing at a running Temporal).

Uses psutil to report process RSS (resident set size). Reports:
- RSS baseline (after imports)
- RSS after BaseApplication()
- RSS after _setup_server()
- RSS idle (server up 2s)

Target: idle RSS under ~100 MiB (achieved via deferred Temporal client and lazy DuckDB).
"""

import asyncio
import os
import sys

# Set SERVER mode before any application_sdk imports so only the API server starts
os.environ["APPLICATION_MODE"] = "SERVER"

import psutil  # noqa: E402

# Add project root for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application_sdk.application import BaseApplication  # noqa: E402
from application_sdk.handlers.base import BaseHandler  # noqa: E402
from application_sdk.workflows import WorkflowInterface  # noqa: E402


class NoOpHandler(BaseHandler):
    """Minimal handler for measurement."""

    async def prepare(self, credentials: dict, **kwargs) -> None:
        pass

    async def test_auth(self, **kwargs) -> bool:
        return True


class NoOpWorkflow(WorkflowInterface):
    """Minimal workflow for measurement."""

    async def run(self, workflow_config: dict) -> None:
        pass


def get_rss_mb() -> float:
    """Return current process RSS in MiB."""
    return psutil.Process().memory_info().rss / (1024 * 1024)


async def main() -> None:
    rss_baseline_mb = get_rss_mb()

    app = BaseApplication(
        name="measure-memory-app",
        server=None,
        application_manifest=None,
        handler_class=NoOpHandler,
    )
    rss_after_app_mb = get_rss_mb()

    # Build server only (same as APPLICATION_MODE=SERVER path), then run serve in background
    await app._setup_server(
        workflow_class=NoOpWorkflow,
        ui_enabled=False,
        has_configmap=False,
    )
    rss_after_setup_mb = get_rss_mb()

    # Run server in background, sample memory after it has settled
    server_task = asyncio.create_task(app.server.start())
    await asyncio.sleep(2.0)
    rss_idle_mb = get_rss_mb()

    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass

    print(f"RSS baseline (after imports):     {rss_baseline_mb:.1f} MiB")
    print(f"RSS after BaseApplication():     {rss_after_app_mb:.1f} MiB")
    print(f"RSS after _setup_server():       {rss_after_setup_mb:.1f} MiB")
    print(f"RSS idle (server up 2s):         {rss_idle_mb:.1f} MiB")


if __name__ == "__main__":
    asyncio.run(main())
