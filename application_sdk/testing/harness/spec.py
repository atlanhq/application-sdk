"""Identity of the app a harness run targets in a cluster.

Renamed from ``application_sdk.testing.e2e.config.AppConfig``, which collided by
name with the production :class:`application_sdk.main.AppConfig` — the
authoritative runtime config object. Two unrelated types with one name is
tolerable while one of them is unexported plumbing; it stops being tolerable the
moment the name becomes shared vocabulary across the harness, the runtime
scenario suite and the connector suites.

Four of the original seven fields (``app_module``, ``image``,
``worker_health_port``, ``timeout``) were never read by anything — not by the
harness, not by ``tests/e2e/conftest.py``'s helpers, not by any consumer repo —
so they are dropped here rather than carried into the shared vocabulary. The
deprecated :class:`~application_sdk.testing.e2e.config.AppConfig` alias still
accepts them so an existing ``AppConfig(...)`` call site keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AppUnderTest"]


@dataclass(frozen=True, slots=True)
class AppUnderTest:
    """Where to find the app under test inside a cluster.

    Attributes:
        app_name: Kubernetes resource prefix. The handler Service is
            ``{app_name}-handler``; the worker Deployment is ``{app_name}-worker``.
        namespace: Kubernetes namespace the app is deployed into.
        handler_port: Port the handler Service listens on.
    """

    app_name: str
    namespace: str
    handler_port: int = 8000
