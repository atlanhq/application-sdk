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
"""

from __future__ import annotations

import concurrent.futures
from typing import Any

from fastapi import FastAPI

from application_sdk._runtime.offload import blocking_executor
from application_sdk.handler.base import Handler
from application_sdk.handler.routes import (
    LoggingPreflightObserver,
    register_handler_routes,
)

#: Threads in each app's ``run_in_thread`` pool on the host.
DEFAULT_BLOCKING_THREADS = 8


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
) -> FastAPI:
    """A FastAPI app serving ``handler`` as ``app_name``.

    There is no secret store on the host, so a handler's ``get_secret`` raises
    ``SecretStoreNotConfiguredError``; request credentials work as on the worker.
    ``run_in_thread`` calls in its requests run on the app's own pool of
    ``blocking_threads`` threads, kept on ``app.state.blocking_executor`` for
    the host to shut down.
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
    )
    return app
