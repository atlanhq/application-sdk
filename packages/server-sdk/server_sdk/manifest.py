"""The ``/manifest`` family — the DAG template the Automation Engine submits from.

Registered automatically by :func:`server_sdk.server.build_asgi_app` for any app
whose generated contract directory contains manifests, so an app gets the routes
without writing code. Apps that need request-time DAG shaping pass a
``compute_manifest`` mapping (entrypoint name -> async hook).

This is load-bearing and easy to get subtly wrong, so the two traps are spelled
out here.

**Trap 1 — the route must EXIST.** heracles calls ``POST <app>/manifest``
(``heracles/pkg/app/client.go:910``) and falls back to the legacy GET **only on
405**; the comment at ``:915-917`` explicitly rejects 404 as a fallback trigger,
because application-sdk's POST route also 404s for an unknown entrypoint. So a
server with no ``/manifest`` route at all returns 404, heracles cannot recover,
and every AE submit for this app hard-fails. We register **GET**, exactly as
application-sdk does — Starlette then answers a POST to that path with 405 on
its own, which is precisely the shape heracles expects and recovers from.

**Trap 2 — the task queue must not be derived from the process's app name.**
The worker polls ``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}``, and
in the consolidated host ``ATLAN_APPLICATION_NAME`` is the *host's* name
(``common-api-server``), not this app's. Reading it here would emit a manifest
pointing at a queue nobody polls: AE would accept the submit, report success,
and the workflow would hang unconsumed forever. So the app name is the
``app_name`` the app itself passed to :func:`build_asgi_app` — never an
environment lookup. This is the single most important line in this module, and
the same defect was found independently in three of the first apps migrated.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Same constraint as the @entrypoint decorator and application-sdk's dispatcher.
# Applied BEFORE any filesystem path is built — this is the path-traversal guard.
ENTRYPOINT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")

ComputeManifest = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


def worker_task_queue(app_name: str) -> str:
    """The queue this app's worker actually polls: ``atlan-{app}-{deployment}``.

    ``app_name`` is passed in from the package constant — deliberately NOT read
    from ``ATLAN_APPLICATION_NAME``, which in the consolidated host names the
    host rather than the hosted app (see the module docstring, trap 2).
    """
    deployment = os.environ.get("ATLAN_DEPLOYMENT_NAME", "local")
    return f"atlan-{app_name}-{deployment}"


def _deployment_name() -> str:
    return os.environ.get("ATLAN_DEPLOYMENT_NAME") or "default"


def _manifest_registry(generated_dir: Path) -> dict[str, Path]:
    """Map entrypoint name → its ``manifest.json``, discovered by glob.

    Built from ``generated_dir.glob("*/manifest.json")`` rather than by joining
    a caller-supplied name onto a path, so a hostile ``entrypoint`` can never
    escape the directory even if the regex guard above were bypassed.
    """
    return {p.parent.name: p for p in sorted(generated_dir.glob("*/manifest.json"))}


def _parse_fe_inputs(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="fe_inputs is not valid JSON"
        ) from None
    return parsed if isinstance(parsed, dict) else {}


def register_manifest_routes(
    app: FastAPI,
    *,
    app_name: str,
    generated_dir: Path,
    compute_manifest: dict[str, ComputeManifest] | None = None,
) -> None:
    """Register ``GET /manifest`` and ``GET /workflows/v1/manifest``.

    POST is intentionally not registered: Starlette answers it with 405, which
    is what heracles falls back on (module docstring, trap 1).
    """
    hooks = compute_manifest or {}
    # Built once: the manifests are baked into the image, and a synchronous glob
    # per request shares its latency with every co-hosted app and the kubelet
    # probes on the one event loop.
    registry = _manifest_registry(generated_dir)

    async def _serve(entrypoint: str | None, fe_inputs: str | None) -> Response:
        if entrypoint:
            if not ENTRYPOINT_NAME_RE.match(entrypoint):
                raise HTTPException(status_code=400, detail="Invalid entrypoint name")
            path = registry.get(entrypoint)
            if path is None:
                raise HTTPException(
                    status_code=404, detail=f"No manifest for entrypoint: {entrypoint}"
                )
        else:
            # No ?entrypoint=: serve a root manifest.json when the app has one,
            # else fall back to the alphabetically-first entrypoint rather than
            # 404-ing, matching application-sdk's pre-validation behaviour.
            root = generated_dir / "manifest.json"
            if root.is_file():
                path, entrypoint = root, ""
            elif registry:
                entrypoint, path = next(iter(sorted(registry.items())))
                logger.info(
                    "manifest requested without ?entrypoint=; falling back to %r",
                    entrypoint,
                )
            else:
                raise HTTPException(status_code=404, detail="No manifest available")

        raw = path.read_bytes().replace(
            b"{deployment_name}", _deployment_name().encode()
        )

        hook = hooks.get(entrypoint) if entrypoint else None
        if hook is None:
            # No per-entrypoint hook: serve the generated bytes unchanged, with
            # only the deployment token substituted (no parse/reserialize — the
            # contract tooling already validated this file at build time).
            return Response(content=raw, media_type="application/json")

        base = json.loads(raw)
        computed = await hook(base, _parse_fe_inputs(fe_inputs))
        return Response(content=json.dumps(computed), media_type="application/json")

    @app.get("/workflows/v1/manifest")
    async def get_manifest(
        entrypoint: str | None = Query(default=None),
        fe_inputs: str | None = Query(default=None),
    ) -> Response:
        return await _serve(entrypoint, fe_inputs)

    @app.post("/manifest", include_in_schema=False)
    async def post_manifest_not_allowed() -> Response:
        """Answer POST explicitly with 405 rather than relying on the router.

        heracles POSTs here first and falls back to the legacy GET only on 405.
        Starlette would produce that 405 by itself — but only while no other
        route fully matches the path: a catch-all mount (ai-memory mounts its MCP
        transport at the ASGI root) is a full match and would turn this into a
        404, which heracles cannot recover from. Registering the method makes the
        405 independent of what else the app mounts.
        """
        return Response(status_code=405, headers={"Allow": "GET"})

    @app.get("/manifest", include_in_schema=False)
    async def get_manifest_legacy(
        entrypoint: str | None = Query(default=None),
        fe_inputs: str | None = Query(default=None),
    ) -> Response:
        """Unversioned alias heracles uses; POST to it yields 405 by design."""
        return await _serve(entrypoint, fe_inputs)
