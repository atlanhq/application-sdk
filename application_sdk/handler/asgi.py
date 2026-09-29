"""Serve a :class:`~application_sdk.handler.base.Handler` without the worker.

The consolidated API host installs only the api distribution plus each app's
handler package, and mounts every handler with :func:`build_asgi_app`. The
auth / check / metadata routes are the ones the worker's handler service
registers (:func:`~application_sdk.handler.routes.register_handler_routes`), so
an app answers the same way from either place.
"""

from __future__ import annotations

from fastapi import FastAPI

from application_sdk.handler.base import Handler
from application_sdk.handler.routes import (
    LoggingPreflightObserver,
    register_handler_routes,
)


def build_asgi_app(
    handler: Handler, *, app_name: str, app_package: str = "app"
) -> FastAPI:
    """A FastAPI app serving ``handler`` as ``app_name``.

    There is no secret store on the host, so a handler's ``get_secret`` raises
    ``SecretStoreNotConfiguredError``; request credentials work as on the worker.
    """
    app = FastAPI(title=app_name)
    app.state.app_name = app_name

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
