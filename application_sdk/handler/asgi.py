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
queue is the one the app's worker polls: its ``ATLAN_TASK_QUEUE`` override when
the host's app-env sync carries one (``ATLAN_APPENV__<APP>__ATLAN_TASK_QUEUE``),
else ``derive_task_queue`` over the app's name and its deployment name, read as
``ATLAN_APPENV__<APP>__ATLAN_DEPLOYMENT_NAME`` before ``ATLAN_DEPLOYMENT_NAME``.
"""

from __future__ import annotations

import concurrent.futures
import functools
import importlib
import os
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI

from application_sdk._logging import get_logger
from application_sdk._runtime.offload import blocking_executor
from application_sdk.common.task_queue import derive_task_queue
from application_sdk.handler.base import Handler
from application_sdk.handler.metadata_routes import (
    MetadataSource,
    generated_dir_of,
    register_metadata_routes,
)
from application_sdk.handler.object_routes import (
    S3ObjectStore,
    host_object_source,
    register_object_routes,
)
from application_sdk.handler.routes import (
    LoggingPreflightObserver,
    include_app_routers_on,
    register_handler_routes,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = get_logger(__name__)

#: Threads in each app's ``run_in_thread`` pool on the host.
DEFAULT_BLOCKING_THREADS = 8

#: The host's per-app environment prefix: ``ai-memory`` → ``ATLAN_APPENV__AI_MEMORY__``.
APPENV_PREFIX = "ATLAN_APPENV__"


def app_env(app_name: str, key: str, default: str = "") -> str:
    """``key`` for one hosted app: its ``ATLAN_APPENV__<APP>__<key>``, else
    ``default``. An empty string counts as set.

    Never the bare ``key``: on the host that is the host's own value, shared by
    every hosted app, so reading it would hand one app another app's (or the
    host's) setting.
    """
    scoped = APPENV_PREFIX + app_name.strip().upper().replace("-", "_").replace(
        ".", "_"
    )
    return os.environ.get(f"{scoped}__{key}", default)


@functools.cache
def default_object_store() -> S3ObjectStore | None:
    """The host's object store, built once by :meth:`S3ObjectStore.from_env`.

    One bucket and one client for every hosted app: each app's configs live
    under its own ``persistent-artifacts/apps/<app>/`` prefix in that bucket.
    """
    return S3ObjectStore.from_env()


class _Unset(Enum):
    """``build_asgi_app(object_store=...)`` default: build the store from env."""

    FROM_ENV = auto()


def _task_queue(app_name: str, deployment: Callable[[], str]) -> str | None:
    """The queue the app's worker polls, for the served manifest.

    Stamped, not derived, wherever the host can be told: the worker's own
    ``ATLAN_TASK_QUEUE`` override, which the host's app-env sync carries as
    ``ATLAN_APPENV__<APP>__ATLAN_TASK_QUEUE``. Never the host's bare
    ``ATLAN_TASK_QUEUE``: an override names one app's queue, and the host serves
    many. Otherwise derived from the app's name and *deployment*, as the worker
    derives it (see :func:`_deployment_name`). With no deployment name either,
    ``None``: the manifest keeps its baked queue and this is logged, rather than
    a made-up queue no worker polls.
    """
    override = app_env(app_name, "ATLAN_TASK_QUEUE").strip()
    if override:
        return override
    deployment_name = deployment()
    if not deployment_name:
        logger.error(
            "No ATLAN_APPENV__<APP>__ATLAN_TASK_QUEUE and no deployment name for "
            "hosted app %s, so the served manifest cannot be stamped with the "
            "queue its worker polls. Set ATLAN_DEPLOYMENT_NAME on the host.",
            app_name,
        )
        return None
    return derive_task_queue(app_name, deployment_name)


def _deployment_name(app_name: str) -> str:
    """The deployment name *app_name*'s worker derives its queue from.

    The app's own ``ATLAN_APPENV__<APP>__ATLAN_DEPLOYMENT_NAME`` when set, else
    the host's ``ATLAN_DEPLOYMENT_NAME``. The fallback is deliberate, unlike the
    queue override: the atlan-app chart gives every app's worker and the host the
    same tenant-wide ``deploymentName``, so the host's value is the workers'
    value. A Self-Deployed Runtime run polls a different queue, but Heracles
    re-stamps every agent-side node with ``atlan-<sdr deployment>`` on create and
    update (``stampSDRTaskQueue``), so the served queue only ever reaches the
    cloud-side nodes, which use the tenant's deployment name.
    """
    return (
        app_env(app_name, "ATLAN_DEPLOYMENT_NAME").strip()
        or os.environ.get("ATLAN_DEPLOYMENT_NAME", "").strip()
    )


def _package_generated_dir(app_package: str) -> Path | None:
    """The ``generated/`` folder shipped inside *app_package*, if it has one.

    ``None`` when the package itself is not installed (logged): the app is served
    without metadata routes. Any other import failure — the package exists but
    its own code or a dependency fails to import — is a real error and raises.
    """
    try:
        module = importlib.import_module(app_package)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if not missing or not (
            app_package == missing or app_package.startswith(f"{missing}.")
        ):
            raise
        logger.warning(
            "App package %s is not importable (module %s not found), so its "
            "metadata routes are not served",
            app_package,
            missing,
            exc_info=True,
        )
        return None
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return None
    generated = generated_dir_of(Path(module_file).parent)
    return generated if generated.is_dir() else None


class _AppBlockingExecutor:
    """ASGI middleware: ``run_in_thread`` in this app's requests uses its pool."""

    def __init__(self, app: ASGIApp, executor: concurrent.futures.Executor) -> None:
        self.app = app
        self.executor = executor

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with blocking_executor(self.executor):
            await self.app(scope, receive, send)


def build_asgi_app(
    handler: Handler,
    *,
    app_name: str,
    app_package: str = "app",
    blocking_threads: int = DEFAULT_BLOCKING_THREADS,
    generated_dir: Path | None = None,
    object_store: S3ObjectStore | None | Literal[_Unset.FROM_ENV] = _Unset.FROM_ENV,
) -> FastAPI:
    """A FastAPI app serving ``handler`` as ``app_name``.

    There is no secret store on the host, so a handler's ``get_secret`` raises
    ``SecretStoreNotConfiguredError``; request credentials work as on the worker.
    ``run_in_thread`` calls in its requests run on the app's own pool of
    ``blocking_threads`` threads, kept on ``app.state.blocking_executor`` for
    the host to shut down. The metadata routes are served when the app ships a
    generated folder (``generated_dir``, else ``<app_package>/generated``). The
    config and file routes use ``object_store`` (by default
    :func:`default_object_store`) and answer 503 without one.
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
            return _deployment_name(app_name)

        register_metadata_routes(
            app,
            MetadataSource(
                generated_dir=lambda: generated,
                app_name=lambda: app_name,
                app_package=app_package,
                task_queue=lambda: _task_queue(app_name, _deployment),
                deployment_name=_deployment,
                from_process_env=False,
            ),
        )
    store = default_object_store() if object_store is _Unset.FROM_ENV else object_store
    register_object_routes(app, host_object_source(app_name, store))
    # The app's own routers go last, so one that redefines an SDK route is refused.
    include_app_routers_on(app, handler, app_name)
    return app
