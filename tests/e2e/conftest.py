"""Pytest fixtures for K8s e2e tests.

Override ``app_config`` in a downstream repo's ``conftest.py`` to deploy
a specific app. The ``deployed_app`` session fixture handles deploy/undeploy
and log collection automatically.

Example downstream conftest::

    @pytest.fixture(scope="session")
    def app_config() -> AppConfig:
        return AppConfig(
            app_name="my-app",
            app_module="my_app.main:App",
            namespace="app-my-app",
            image="ghcr.io/my-org/my-app:latest",
        )
"""

import os
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing.e2e import (
    AppConfig,
    AppDeployer,
    LogCollector,
    kube_http_call,
)


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    """Return AppConfig for the app under test.

    Override this fixture in your repo's conftest.py. By default, reads
    from environment variables so CI can inject values without code changes.
    """
    return AppConfig(
        app_name=os.environ["E2E_APP_NAME"],
        app_module=os.environ["E2E_APP_MODULE"],
        namespace=os.environ.get("E2E_NAMESPACE", f"app-{os.environ['E2E_APP_NAME']}"),
        image=os.environ["E2E_IMAGE"],
        timeout=int(os.environ.get("E2E_TIMEOUT", "300")),
    )


@pytest.fixture(scope="session")
async def deployed_app(app_config: AppConfig) -> AsyncGenerator[AppDeployer, None]:
    """Deploy the app, yield the deployer, then collect logs and undeploy."""
    deployer = AppDeployer(app_config)
    await deployer.deploy()
    await deployer.wait_ready()
    try:
        yield deployer
    finally:
        collector = LogCollector(app_config.namespace, Path("test-logs"))
        await collector.collect()
        await collector.collect_events()
        await deployer.undeploy()


@pytest.fixture
def handler_call(deployed_app: AppDeployer) -> Callable[..., Any]:
    """Return an async callable that routes requests to the handler via port-forward."""
    config = deployed_app.config

    async def _call(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        return await kube_http_call(
            namespace=config.namespace,
            service=f"{config.app_name}-handler",
            port=config.handler_port,
            method=method,
            path=path,
            body=body,
            timeout=timeout,
        )

    return _call
