"""AppConfig dataclass for K8s e2e test deployments."""

from dataclasses import dataclass, field


@dataclass
class AppConfig:
    """Configuration for deploying an app to K8s for e2e testing.

    Args:
        app_name: Helm release name and K8s resource prefix.
        app_module: Python module path, e.g. ``"my_app.main:App"``.
        namespace: K8s namespace to deploy into.
        image: Full image reference, e.g. ``"ghcr.io/org/my-app:v1.0.0"``.
            Used to derive ``helm_values`` image overrides (registry/repository/tag).
        helm_values: Additional ``--set`` overrides passed to ``helm upgrade``.
            These take precedence over image-derived values.
        handler_port: Port the handler service listens on.
        worker_health_port: Port the worker health server listens on.
        timeout: Helm deploy timeout in seconds.
    """

    app_name: str
    app_module: str
    namespace: str
    image: str
    helm_values: dict[str, str] = field(default_factory=dict)
    handler_port: int = 8080
    worker_health_port: int = 8081
    timeout: int = 300
