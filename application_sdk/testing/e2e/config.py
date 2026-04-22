"""AppConfig dataclass for K8s e2e test deployments."""

from dataclasses import dataclass


@dataclass
class AppConfig:
    """Configuration for an app under K8s e2e testing.

    Args:
        app_name: K8s resource prefix.
        app_module: Python module path, e.g. ``"my_app.main:App"``.
        namespace: K8s namespace to deploy into.
        image: Full image reference, e.g. ``"ghcr.io/org/my-app:v1.0.0"``.
        handler_port: Port the handler service listens on.
        worker_health_port: Port the worker health server listens on.
        timeout: Deploy timeout in seconds.
    """

    app_name: str
    app_module: str
    namespace: str
    image: str
    handler_port: int = 8000
    worker_health_port: int = 8081
    timeout: int = 300
