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


@pytest.fixture(scope="session", autouse=True)
def _integration_env_vars():
    """Disable interceptors and Dapr sinks for the integration test session.

    Using a session-scoped fixture rather than module-level os.environ calls
    prevents these settings from leaking into unit tests when the full test
    suite is run with ``pytest tests/``.  The module-level pattern runs at
    conftest import time (before any tests) and poisons the process env for
    tests in other directories that rely on the defaults.
    """
    overrides = {
        "ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK": "false",
        "APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR": "false",
        "APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR": "false",
    }
    old = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


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
def reregister_app():
    """Return a helper that re-adds an App class to AppRegistry and TaskRegistry.

    clean_registries (autouse) resets both registries before each test.
    Call reregister_app(AppClass) before spinning up a worker for any
    module-level App class.
    """

    def _reregister(app_cls: type) -> None:
        from application_sdk.app.base import _register_tasks
        from application_sdk.app.registry import AppRegistry

        AppRegistry.get_instance().register(
            name=app_cls._app_name,
            version=app_cls.version,
            app_cls=app_cls,
            input_type=app_cls._input_type,
            output_type=app_cls._output_type,
            entry_points=app_cls._app_metadata.entry_points,
            allow_override=True,
        )
        _register_tasks(app_cls, app_cls._app_name)

    return _reregister


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
