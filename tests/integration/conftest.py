"""Integration test configuration and shared fixtures.

Temporal and Dapr are provisioned **in-process** by the SDK's own embedded
dev helpers (``application_sdk.dev.embedded_runtime`` /
``embedded_dapr``) — the same code path local development uses. No external
``temporal server start-dev`` or ``daprd`` is required; the binaries are
fetched to ``~/.cache`` on first use and reused thereafter.

Set ``TEMPORAL_HOST`` to point the suite at an already-running Temporal
server instead of booting an embedded one (handy when iterating locally
against ``uv run poe start-deps``).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _integration_env_vars():
    """Disable interceptors and Dapr sinks for the duration of each integration test.

    Function-scoped — setup runs before every integration test, teardown
    restores ``os.environ`` after each test. Function scope is required
    because session-scoped autouse would activate for the first integration
    test that runs and leak its env-var mutations to every later test in
    the same ``pytest tests/`` session (including unit tests that follow,
    if any integration test reaches the fixture before the unit job's
    ``test_on_complete`` tests run — see BLDX-1283 PR fallout).
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


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_host():
    """``host:port`` of an embedded Temporal dev server for the whole session.

    Booted once per test session via the SDK's :func:`embedded_runtime`
    (same code path as local development); the dev-server binary is fetched
    to ``~/.cache`` on first use. Yields only the *host string* — which has
    no event-loop affinity — so each function-scoped :func:`temporal_client`
    can build its own client on its own loop. If ``TEMPORAL_HOST`` is set,
    that external server is used instead and nothing is booted.
    """
    host_override = os.environ.get("TEMPORAL_HOST")
    if host_override:
        yield host_override
        return

    from application_sdk.dev import embedded_runtime

    async with embedded_runtime() as runtime:
        yield runtime.host


@pytest.fixture
async def temporal_client(temporal_host):
    """Temporal client connected to the session's embedded dev server.

    Connects via :func:`create_temporal_client` so the client keeps its
    production pydantic data converter — only the *host* comes from the
    shared embedded runtime.
    """
    from application_sdk.execution._temporal.backend import create_temporal_client

    return await create_temporal_client(temporal_host, connect_max_attempts=1)


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


# Secrets the Dapr HTTP tests assert against. File-backed (see ``secrets=``
# below) so hyphenated keys round-trip — ``local.env`` mangles them.
_DAPR_TEST_SECRETS = {
    "test-secret": "integration-test-value",
    "api-key": "test-api-key-12345",
    "db-password": "test-db-password",
}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def embedded_dapr_sidecar():
    """Embedded ``daprd`` for the module, provisioned in-process by the SDK.

    Uses :func:`embedded_dapr` (same code path as local development); the
    ``daprd`` binary is fetched to ``~/.cache`` on first use. On entry it
    sets ``DAPR_HTTP_PORT`` etc. in the environment, so an
    :class:`AsyncDaprClient` constructed with no explicit base URL connects
    to it automatically. Yields the :class:`EmbeddedDapr` connection info
    (no event-loop affinity), booted once per test module.
    """
    from application_sdk.dev import embedded_dapr

    async with embedded_dapr(secrets=_DAPR_TEST_SECRETS) as dapr:
        yield dapr


# =============================================================================
# HTTP Integration Test Fixtures (connector integration testing framework)
# =============================================================================


@pytest.fixture(scope="session")
def server_host() -> str:
    """Get the application server host from environment.

    Returns:
        str: The server URL (default: http://localhost:8000).
    """
    return os.getenv("APP_SERVER_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def integration_test_config() -> dict:
    """Get integration test configuration from environment.

    Returns:
        dict: Configuration dictionary with server_host, version, endpoint, timeout.
    """
    return {
        "server_host": os.getenv("APP_SERVER_URL", "http://localhost:8000"),
        "server_version": os.getenv("APP_SERVER_VERSION", "v1"),
        "workflow_endpoint": os.getenv("WORKFLOW_ENDPOINT", "/start"),
        "timeout": int(os.getenv("INTEGRATION_TEST_TIMEOUT", "30")),
    }


def load_credentials_from_env(prefix: str) -> dict:
    """Load credentials from environment variables with a given prefix.

    Collects all environment variables that start with ``{PREFIX}_``
    and returns them as a dict with lowercase keys.

    Args:
        prefix: The environment variable prefix (e.g., "POSTGRES").

    Returns:
        dict: Credentials dictionary.
    """
    credentials: dict = {}
    prefix_upper = prefix.upper()

    for key, value in os.environ.items():
        if key.startswith(f"{prefix_upper}_"):
            cred_key = key[len(prefix_upper) + 1 :].lower()
            credentials[cred_key] = value

    return credentials


@pytest.fixture
def load_creds():
    """Fixture returning the :func:`load_credentials_from_env` helper."""
    return load_credentials_from_env
