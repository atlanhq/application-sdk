"""Pytest fixtures for K8s e2e tests.

Override ``app_config`` in a downstream repo's ``conftest.py`` to configure
a specific app. Utility fixtures provide log collection and HTTP helpers.

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
from collections.abc import Callable
from typing import Any

import pytest

from application_sdk.testing.e2e import AppConfig, kube_http_call


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


@pytest.fixture
def handler_call(app_config: AppConfig) -> Callable[..., Any]:
    """Return an async callable that routes requests to the handler via port-forward."""

    async def _call(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        return await kube_http_call(
            namespace=app_config.namespace,
            service=f"{app_config.app_name}-handler",
            port=app_config.handler_port,
            method=method,
            path=path,
            body=body,
            timeout=timeout,
        )

    return _call
