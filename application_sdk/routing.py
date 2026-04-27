"""Multi-app HTTP routing for the consolidated runtime (`common-app-server`).

This module provides the SDK-side primitives that let many apps share a single
process. Each app exposes its own router (a FastAPI/ASGI sub-app) from its
package; the consolidated runtime calls :func:`host_apps` to mount them under
Host-header dispatch.

Layout:

    Per-app package
    ---------------
    Each app exposes ``server_router`` as an importable attribute::

        # in {app_pkg}/router.py
        from application_sdk.handler.service import create_app_handler_service

        server_router = create_app_handler_service(my_handler, app_name="my-app")

    Consolidated runtime (``common-app-server``)
    --------------------------------------------
    The runtime's main module imports each app's router and registers them::

        from application_sdk.routing import host_apps

        from publish_app.router  import server_router as publish_router
        from redshift_app.router import server_router as redshift_router

        app = host_apps([
            ("publish",  publish_router),
            ("redshift", redshift_router),
        ])

K8s convention: each ``(name, router)`` tuple registers a Host pattern of
``{name}.{shared_namespace}.svc.cluster.local`` (and bare ``{name}`` for short-form
in-cluster lookups). The shared namespace defaults to ``"common-app-server"`` and
can be overridden via the ``shared_namespace`` keyword argument or the
``ATLAN_COMMON_APP_NAMESPACE`` environment variable.

Validated behavior (see ``tests/unit/test_routing.py``):

    1. Sub-apps with identical paths coexist; Host header is the discriminator.
    2. HTTP Host headers are matched case-insensitively (RFC 7230).
    3. Port-suffixed Hosts (``host:80``) match the bare-host pattern.
    4. Pod-level ``/health`` is reachable via a parent route registered AFTER
       Host routes — so it answers when the Host doesn't match any app
       (e.g. the kubelet liveness probe arrives with ``Host = <pod-ip>``).
    5. Unknown Hosts return a clean 404, never a silent dispatch.

Critical invariant: Host routes MUST be registered before any parent-level
path routes (Starlette matches in order; a parent path will shadow Host
dispatch otherwise).
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Sequence

from fastapi import FastAPI
from starlette.routing import Host
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_SHARED_NAMESPACE = "common-app-server"

#: Per-request app identity. Set by :class:`HostDispatchMiddleware` based on the
#: ``Host`` header. Code that needs to know which app is handling the current
#: request — for logging, metrics, OTEL service name, etc. — reads from this
#: ContextVar instead of the legacy ``application_sdk.constants.APPLICATION_NAME``
#: (which is a process-level constant that can't disambiguate apps in a shared
#: runtime).
current_app_name: ContextVar[str | None] = ContextVar("current_app_name", default=None)


class LowercaseHostMiddleware:
    """ASGI middleware that lowercases the ``Host`` header before routing.

    HTTP ``Host`` headers are case-insensitive per RFC 7230 §5.4, but Starlette's
    :class:`~starlette.routing.Host` route compares strings exactly. Without
    this middleware, a client that sends ``Host: PUBLISH.common-app-server...``
    would 404 even though the lowercase pattern is registered. Normalising
    upstream keeps dispatch robust to client variation.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            new_headers = [
                (name, value.lower() if name == b"host" else value)
                for name, value in scope["headers"]
            ]
            scope = dict(scope)
            scope["headers"] = new_headers
        await self.app(scope, receive, send)


class HostDispatchContextMiddleware:
    """ASGI middleware that sets :data:`current_app_name` based on the Host.

    Runs after :class:`LowercaseHostMiddleware` (so it sees a normalised Host)
    and before request handlers. The mapping comes from the same ``apps`` list
    passed to :func:`host_apps`, so it stays in sync with the registered Host
    routes by construction.
    """

    def __init__(self, app: ASGIApp, host_to_name: dict[str, str]) -> None:
        self.app = app
        self.host_to_name = host_to_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = _extract_host(scope)
            name = self.host_to_name.get(host)
            if name is not None:
                token = current_app_name.set(name)
                try:
                    await self.app(scope, receive, send)
                finally:
                    current_app_name.reset(token)
                return
        await self.app(scope, receive, send)


def _extract_host(scope: Scope) -> str:
    """Pull the bare hostname (no port) from the ASGI scope's Host header."""
    for name, value in scope.get("headers", ()):
        if name == b"host":
            host = value.decode("latin-1")
            return host.split(":", 1)[0]
    return ""


def host_apps(
    apps: Sequence[tuple[str, FastAPI]],
    *,
    shared_namespace: str | None = None,
) -> ASGIApp:
    """Build the consolidated parent FastAPI that dispatches to per-app routers.

    Args:
        apps: Sequence of ``(k8s_name, router)`` tuples. ``k8s_name`` is the
            value of ``.Values.name`` in the production Helm chart for that app
            (e.g. ``"publish"``, ``"redshift"``). ``router`` is any ASGI app —
            typically a :class:`fastapi.FastAPI` sub-app produced by the SDK's
            ``create_app_handler_service``.
        shared_namespace: K8s namespace where the consolidated runtime's
            Services live. Defaults to the value of the
            ``ATLAN_COMMON_APP_NAMESPACE`` env var, falling back to
            ``"common-app-server"``.

    Returns:
        An ASGI application that dispatches incoming HTTP requests to the
        correct sub-app based on the ``Host`` header. Pod-level ``/health``
        (kubelet liveness probes hitting the pod IP) is also exposed.

    The returned app accepts any of these Host patterns for an app named
    ``"publish"``:

    - ``publish.common-app-server.svc.cluster.local``  (FQDN)
    - ``publish.common-app-server``                    (cluster-DNS short form)
    - ``publish``                                      (in-namespace short form)

    Each is registered with optional ``:port`` suffix tolerance via Starlette's
    built-in Host matching.

    Raises:
        ValueError: if ``apps`` contains duplicate ``k8s_name`` values.
    """
    namespace = (
        shared_namespace
        or os.environ.get("ATLAN_COMMON_APP_NAMESPACE")
        or DEFAULT_SHARED_NAMESPACE
    )

    seen_names: set[str] = set()
    host_to_name: dict[str, str] = {}

    parent = FastAPI()

    # Order matters: register Host routes BEFORE any parent-level path routes.
    # Starlette matches routes in order; if /health on the parent comes first,
    # it shadows per-app /health under Host dispatch. Verified by the
    # ``per-app /health via Host header`` test in tests/unit/test_routing.py.
    for k8s_name, router in apps:
        if k8s_name in seen_names:
            raise ValueError(f"duplicate app name in host_apps(): {k8s_name!r}")
        seen_names.add(k8s_name)

        for host in _host_patterns_for(k8s_name, namespace):
            parent.routes.append(Host(host, app=router))
            host_to_name[host] = k8s_name

    @parent.get("/health")
    async def pod_health() -> dict[str, str]:
        """Pod-level liveness — answers when no app's Host route matched."""
        return {"status": "ok", "scope": "pod"}

    # Wrap with the two ASGI middlewares. Order: lowercase first (so context
    # sees a normalised host), then context (so request handlers see
    # current_app_name set).
    wrapped: ASGIApp = HostDispatchContextMiddleware(parent, host_to_name)
    wrapped = LowercaseHostMiddleware(wrapped)
    return wrapped


def _host_patterns_for(k8s_name: str, namespace: str) -> list[str]:
    """Return the Host-header values to register for ``k8s_name``.

    Three forms are registered to cover the realistic ways an in-cluster
    client constructs a URL: full FQDN, cluster-DNS short form, and
    in-namespace bare hostname.
    """
    return [
        f"{k8s_name}.{namespace}.svc.cluster.local",
        f"{k8s_name}.{namespace}",
        k8s_name,
    ]
