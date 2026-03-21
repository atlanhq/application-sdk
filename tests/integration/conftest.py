"""Integration test configuration and shared fixtures.

Requires a running Temporal dev server:
    temporal server start-dev

Set TEMPORAL_HOST env var to override the default localhost:7233.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

# Disable Dapr and observability sinks — integration tests only need Temporal.
os.environ.setdefault("ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK", "false")
os.environ.setdefault("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "false")
os.environ.setdefault("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "false")


@pytest.fixture(scope="session")
def task_queue() -> str:
    """Unique task queue name for this test session."""
    return f"integ-test-{uuid4().hex[:8]}"


@pytest.fixture
async def temporal_client():
    """Temporal client connected to the dev server."""
    from application_sdk.execution._temporal.backend import create_temporal_client

    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    return await create_temporal_client(host, connect_max_attempts=1)


@pytest.fixture(autouse=True)
def clean_registries():
    """Reset AppRegistry and TaskRegistry before and after each test."""
    from application_sdk.app.registry import AppRegistry, TaskRegistry

    AppRegistry.reset()
    TaskRegistry.reset()
    yield
    AppRegistry.reset()
    TaskRegistry.reset()


@pytest.fixture
def run_worker(temporal_client, task_queue):
    """Return a factory for async worker context managers.

    Usage::

        async with run_worker():
            result = await executor.execute(MyApp, ...)
    """
    from application_sdk.execution._temporal.worker import create_worker

    @asynccontextmanager
    async def _ctx(**kwargs):
        worker = create_worker(
            temporal_client,
            task_queue,
            passthrough_modules={"tests"},
            **kwargs,
        )
        async with worker:
            yield

    return _ctx


@pytest.fixture
async def executor(temporal_client, task_queue):
    """Pre-configured TemporalExecutorBackend."""
    from application_sdk.execution._temporal.backend import TemporalExecutorBackend

    return TemporalExecutorBackend(temporal_client, task_queue)
