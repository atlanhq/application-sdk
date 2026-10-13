"""Serve a :class:`~application_sdk.handler.base.Handler` without the worker.

The consolidated API host installs only the api distribution plus each app's
handler package, and mounts every handler with :func:`build_asgi_app`. The
auth / check / metadata routes are the ones the worker's handler service
registers (:func:`~application_sdk.handler.routes.register_handler_routes`), so
an app answers the same way from either place.

Each app gets its own pool for ``run_in_thread``. On the worker one app owns the
process, and a pod whose shared pool is exhausted by stuck calls is restarted.
On the host a shared pool would let one app's stuck calls stall every app.
FastAPI's own threadpool (sync endpoints, ``run_in_threadpool``) is still
shared by every app in the process; handler code is async and offloads through
``run_in_thread``.

The metadata routes (configmaps, manifest, input contract) are served from the
``generated/`` folder ``gen_app_api.py`` ships inside the app's package
(:mod:`application_sdk.handler.metadata_routes`). The served manifest's task
queue is the one the app's worker polls: ``derive_task_queue`` over the app's
name and its deployment name, read as ``ATLAN_APPENV__<APP>__ATLAN_DEPLOYMENT_NAME``
before the bare ``ATLAN_DEPLOYMENT_NAME`` (the host's per-app env convention).
"""

from __future__ import annotations

import concurrent.futures
import importlib
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from application_sdk._runtime.offload import blocking_executor
from application_sdk.common.task_queue import derive_task_queue
from application_sdk.handler.base import Handler
from application_sdk.handler.metadata_routes import (
    MetadataSource,
    generated_dir_of,
    register_metadata_routes,
)
from application_sdk.handler.routes import (
    LoggingPreflightObserver,
    include_app_routers_on,
    register_handler_routes,
)

#: Threads in each app's ``run_in_thread`` pool on the host.
DEFAULT_BLOCKING_THREADS = 8

#: The host's per-app environment prefix: ``ai-memory`` → ``ATLAN_APPENV__AI_MEMORY__``.
APPENV_PREFIX = "ATLAN_APPENV__"


def app_env(app_name: str, key: str, default: str = "") -> str:
    """``key`` for one hosted app: its ``ATLAN_APPENV__<APP>__<key>``, else the bare
    ``key``, else ``default``. An empty string counts as set."""
    scoped = APPENV_PREFIX + app_name.strip().upper().replace("-", "_").replace(
        ".", "_"
    )
    value = os.environ.get(f"{scoped}__{key}")
    if value is not None:
        return value
    return os.environ.get(key, default)


def _package_generated_dir(app_package: str) -> Path | None:
    """The ``generated/`` folder shipped inside *app_package*, if it has one."""
    try:
        module = importlib.import_module(app_package)
    except ImportError:
        return None
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return None
    generated = generated_dir_of(Path(module_file).parent)
    return generated if generated.is_dir() else None


class _AppBlockingExecutor:
    """ASGI middleware: ``run_in_thread`` in this app's requests uses its pool."""

    def __init__(self, app: Any, executor: concurrent.futures.Executor) -> None:
        self.app = app
        self.executor = executor

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        with blocking_executor(self.executor):
            await self.app(scope, receive, send)


def build_asgi_app(
    handler: Handler,
    *,
    app_name: str,
    app_package: str = "app",
    blocking_threads: int = DEFAULT_BLOCKING_THREADS,
    generated_dir: Path | None = None,
) -> FastAPI:
    """A FastAPI app serving ``handler`` as ``app_name``.

    There is no secret store on the host, so a handler's ``get_secret`` raises
    ``SecretStoreNotConfiguredError``; request credentials work as on the worker.
    ``run_in_thread`` calls in its requests run on the app's own pool of
    ``blocking_threads`` threads, kept on ``app.state.blocking_executor`` for
    the host to shut down. The metadata routes are served when the app ships a
    generated folder (``generated_dir``, else ``<app_package>/generated``).
    """
    app = FastAPI(title=app_name)
    app.state.app_name = app_name
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=blocking_threads, thread_name_prefix=f"api-{app_name}-"
    )
    app.state.blocking_executor = executor
    app.add_middleware(_AppBlockingExecutor, executor=executor)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    register_handler_routes(
        app,
        handler,
        app_name=app_name,
        app_package=app_package,
        observer=LoggingPreflightObserver(app_name),
        include_app_routers=False,
    )
    generated = (
        generated_dir
        if generated_dir is not None
        else _package_generated_dir(app_package)
    )
    if generated is not None:

        def _deployment() -> str:
            return app_env(app_name, "ATLAN_DEPLOYMENT_NAME").strip()

        register_metadata_routes(
            app,
            MetadataSource(
                generated_dir=lambda: generated,
                app_name=lambda: app_name,
                app_package=app_package,
                task_queue=lambda: derive_task_queue(app_name, _deployment()),
                deployment_name=_deployment,
            ),
        )
    # The app's own routers go last, so one that redefines an SDK route is refused.
    include_app_routers_on(app, handler, app_name)
    return app
