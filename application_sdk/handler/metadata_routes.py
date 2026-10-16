"""The metadata routes, served from an app's generated contract files.

``/workflows/v1/configmap/{id}``, ``/workflows/v1/configmaps``, the manifest
(``/workflows/v1/manifest`` and the unversioned ``/manifest``, GET and POST) and
``/workflows/v1/input-contract`` read only the files the contract toolkit
generates into ``app/generated/``. The worker's handler service and the
consolidated API host (:func:`application_sdk.handler.asgi.build_asgi_app`)
both register them through :func:`register_metadata_routes`, so an app answers
the same from either place.

The two surfaces differ only in what a :class:`MetadataSource` resolves:

* the worker resolves entry points from its app registry, stamps the manifest
  with the task queue its own worker polls, and runs an app's
  ``compute_manifest`` hooks;
* the host has no app registry. It resolves entry points from the generated
  tree, derives the task queue from the app's name and deployment exactly as the
  worker does, and serves static manifests only: ``gen_app_api.py check``
  refuses an app with a ``compute_manifest`` hook, so no hooked app reaches it.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import orjson
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from application_sdk._logging import get_logger
from application_sdk.common._generated_tree import (
    MANIFEST_STEM,
    choose_form_configmap,
    eligible_form_configmaps,
    names_entrypoint,
)
from application_sdk.common.build_identity import (
    BUILD_IDENTITY_CONFIGMAP_ID,
    build_identity,
)
from application_sdk.common.task_queue import resolve_manifest_tokens
from application_sdk.handler.routes import (
    _ENTRYPOINT_NAME_RE,
    _wrap_response,
    entrypoint_module_segment,
)

logger = get_logger(__name__)

# Heracles URL-encodes the whole setup-form payload into ``fe_inputs`` on the
# GET. A typical form is ~1.6 KB decoded and a heavy multi-select (lists of
# qualified names / GUIDs / tags) is ~5 KB — so 8 KB leaves headroom without
# rejecting legitimate forms. Larger payloads use the POST body.
MAX_FE_INPUTS_BYTES = 8192

#: ``async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict``.
ComputeManifest = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]


def decode_fe_inputs(
    raw: str | None, max_bytes: int = MAX_FE_INPUTS_BYTES
) -> dict[str, Any]:
    """Decode the ``fe_inputs`` query payload (JSON, sent URL-encoded by
    Heracles for dynamic-manifest apps). Returns ``{}`` when absent; raises
    ``HTTPException(413)`` when it exceeds ``max_bytes`` and
    ``HTTPException(400)`` when present but malformed."""
    if not raw:
        return {}
    if len(raw.encode("utf-8")) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"fe_inputs exceeds the {max_bytes}-byte limit",
        )
    try:
        # orjson.JSONDecodeError subclasses json.JSONDecodeError.
        parsed = orjson.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"fe_inputs is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="fe_inputs must be a JSON object")
    return parsed


def entrypoint_form(
    generated_dir: Path, entrypoint: str
) -> tuple[Path | None, list[Path]]:
    """The generated setup form served for *entrypoint*, and the files it was
    chosen from.

    Searches both generated layouts: multi-entrypoint apps nest each form under
    ``<generated_dir>/<entrypoint>/``, single-entrypoint apps emit it flat in
    ``generated_dir``. The nested directory wins. Within a directory
    :func:`choose_form_configmap` decides. ``(None, [])`` when no eligible form
    exists.
    """
    for search_dir in (generated_dir / entrypoint, generated_dir):
        candidates = eligible_form_configmaps(search_dir)
        target = choose_form_configmap(candidates, entrypoint)
        if target is not None:
            return target, candidates
    return None, []


def tree_entrypoints(generated_dir: Path) -> list[str]:
    """The entry points the generated tree has a manifest for, sorted.

    Each ``<generated_dir>/<entrypoint>/manifest.json`` is one (the
    multi-entry-point layout). Read from disk: what the host knows of an app's
    entry points without its registry.
    """
    if not generated_dir.is_dir():
        return []
    return sorted(p.parent.name for p in generated_dir.glob("*/manifest.json"))


def tree_default_entrypoint(generated_dir: Path) -> str | None:
    """The entry point a request that names none resolves to, from disk.

    A single-entry-point app (flat ``manifest.json`` or a flat form) resolves
    to ``""``, the app-level entry point. A multi-entry-point app resolves only
    when it has exactly one; otherwise there is no default, as on the worker
    when the builder marks none.
    """
    if not generated_dir.is_dir():
        return None
    if (generated_dir / "manifest.json").is_file() or eligible_form_configmaps(
        generated_dir
    ):
        return ""
    nested = tree_entrypoints(generated_dir)
    return nested[0] if len(nested) == 1 else None


def tree_input_contract(
    generated_dir: Path, app_package: str, entrypoint: str | None
) -> Any:
    """The generated ``AppInputContract`` for *entrypoint*, resolved from disk.

    Looks where the contract toolkit writes it: ``generated/<ep>/_input.py``
    (or the bundle relocation ``<package>/<ep>/_input.py``) for a nested entry
    point, ``generated/_input.py`` for the app-level one. Raises
    ``HTTPException(404)`` when there is none.
    """
    default = tree_default_entrypoint(generated_dir)
    selected = default if entrypoint is None else entrypoint
    if selected is None:
        raise HTTPException(
            status_code=400,
            detail="This app has several entry points; pass ?entrypoint=.",
        )
    nested = selected and (generated_dir / selected / "manifest.json").is_file()
    if nested:
        segment = entrypoint_module_segment(selected)
        candidates = (
            f"{app_package}.generated.{segment}._input",
            f"{app_package}.{segment}._input",
        )
    else:
        candidates = (f"{app_package}.generated._input",)
    for module_path in candidates:
        try:
            module = importlib.import_module(module_path)
        except ImportError:  # conformance: ignore[E008,E014] optional generated module; continue to next candidate
            continue
        contract = getattr(module, "AppInputContract", None)
        if contract is not None and hasattr(contract, "model_json_schema"):
            return contract
    raise HTTPException(status_code=404, detail="Entry point not found")


@dataclass
class MetadataSource:
    """What the metadata routes read for one app.

    Every attribute that can change at runtime is a callable, read per
    request, so a process that reconfigures (or a test that repoints the
    generated dir) is seen without re-registering the routes.

    Args:
        generated_dir: The app's generated contract directory.
        app_name: The app's name: the build-identity configmap reports it, and
            the manifest's ``{app_name}`` token is filled from it.
        app_package: The import root the generated ``_input.py`` modules live
            under (``app`` on the worker, ``atlan_<app>_api`` on the host).
        task_queue: The queue the app's worker polls, stamped into the served
            manifest. ``None`` derives it from the app name and deployment, as
            the worker itself does.
        deployment_name: Fallback for the manifest's ``{deployment_name}``.
        fallback_app_name: Fills ``{app_name}`` when ``app_name()`` is empty.
        default_entrypoint: Entry point a request that names none resolves to,
            raising ``HTTPException`` when there is none; ``None`` reads it
            from the generated tree.
        explicit_entrypoints: Entry points to try, in order, when the default
            has no manifest; ``None`` reads them from the generated tree.
        input_contract: Resolves ``?entrypoint=`` to its input model, raising
            ``HTTPException``; ``None`` resolves from the generated tree.
        compute_manifest: The entry point's ``compute_manifest`` hook, or
            ``None``.
        programmatic_manifest: An ``AppManifest`` built in Python, served when
            no entry point is named.
        log: The logger the routes report through.
        max_fe_inputs_bytes: The GET ``fe_inputs`` cap.
        from_process_env: Whether this process's own ``ATLAN_APPLICATION_NAME``
            / ``ATLAN_DEPLOYMENT_NAME`` describe this app. ``False`` on the
            consolidated API host, where they describe the host.
    """

    generated_dir: Callable[[], Path]
    app_name: Callable[[], str]
    app_package: str = "app"
    task_queue: Callable[[], str | None] = lambda: None
    deployment_name: Callable[[], str] = lambda: ""
    fallback_app_name: str = ""
    default_entrypoint: Callable[[], str] | None = None
    explicit_entrypoints: Callable[[], list[str]] | None = None
    input_contract: Callable[[str | None], Any] | None = None
    compute_manifest: Callable[[str], ComputeManifest | None] = field(
        default=lambda _entrypoint: None
    )
    programmatic_manifest: Any = None
    log: Callable[[], Any] = lambda: logger
    max_fe_inputs_bytes: Callable[[], int] = lambda: MAX_FE_INPUTS_BYTES
    from_process_env: bool = True


def register_metadata_routes(app: FastAPI, source: MetadataSource) -> None:
    """Register the configmap, manifest and input-contract routes on *app*."""

    def _default_entrypoint(log: Any, context: str) -> str | None:
        """The default entry point, or ``None`` (logged) when there is none."""
        if source.default_entrypoint is None:
            ep_name = tree_default_entrypoint(source.generated_dir())
            if ep_name is None:
                log.warning(context, source.app_name())
            return ep_name
        try:
            return source.default_entrypoint()
        except HTTPException:
            # The registry says why there is no default. Warning, not debug:
            # exc_info-worthy telemetry an operator should see in production.
            log.warning(context, source.app_name(), exc_info=True)
            return None

    def _explicit_entrypoints() -> list[str]:
        if source.explicit_entrypoints is not None:
            return source.explicit_entrypoints()
        return tree_entrypoints(source.generated_dir())

    # ------------------------------------------------------------------
    # ConfigMaps
    # ------------------------------------------------------------------

    @app.get("/workflows/v1/configmap/{config_map_id}")
    async def get_configmap(config_map_id: str) -> JSONResponse:
        log = source.log()
        generated_dir = source.generated_dir()
        # 0. The reserved build-identity id (FND-1684).
        #
        # Answered BEFORE the generated-file scan, deliberately: an app that
        # happens to ship `atlan-build-identity.json` would otherwise shadow the
        # one fact only a running process can report — and it would shadow it
        # with a committed file, which cannot tell this build apart from one
        # shipped months ago. Served on THIS route because
        # `/api/service/configmaps/{name}` is already proxied by Heracles.
        #
        # `build_id` is "" for an image that carries no stamp. That is a valid
        # answer, not an error: the reader has to distinguish "this process
        # reports a different build" from "this process cannot report one".
        if config_map_id == BUILD_IDENTITY_CONFIGMAP_ID:
            identity: dict[str, Any] = {
                "build_id": build_identity(),
                "app_name": source.app_name(),
            }
            return JSONResponse(
                content=_wrap_response(
                    cast(
                        "dict[str, Any]",
                        {
                            "kind": "ConfigMap",
                            "apiVersion": "v1",
                            "metadata": {"name": config_map_id},
                            # Same envelope as a real configmap — `data.config`
                            # is a JSON string — with the parsed keys repeated
                            # alongside it for a client that wants them.
                            "data": {
                                "config": orjson.dumps(identity).decode(),
                                **identity,
                            },
                        },
                    ),
                    message="Build identity fetched successfully",
                )
            )

        # 1. Direct match against any generated configmap file stem. The setup
        #    form normally requests the form file by its stem.
        available_configmaps: list[str] = []
        target: Path | None = None
        if generated_dir.exists():
            for json_file in generated_dir.rglob("*.json"):
                available_configmaps.append(json_file.stem)
                if json_file.stem == config_map_id:
                    target = json_file
                    break

        # 2. Default-entrypoint fallback. The marketplace UI sometimes builds
        #    the configmap URL from the app/marketplace id (e.g.
        #    "atlan-snowflake") rather than the form stem. Resolve to the app's
        #    default entrypoint and serve its form. When there is none, or it
        #    has no eligible form, target stays None and the route 404s.
        if target is None:
            ep_name = _default_entrypoint(
                log, "No default entrypoint for app %s; serving configmap 404"
            )
            if ep_name is not None:
                # Searches the nested layout first, then the flat one, so an
                # app-id request resolves for either shape. Within a directory
                # `choose_form_configmap` picks a form named for the entrypoint,
                # else the only eligible file, else the alphabetically first.
                # That last step is a compatibility guess; the warning below is
                # what makes a wrong guess visible before anyone opens the wizard.
                target, candidates = entrypoint_form(generated_dir, ep_name)
                if target is not None:
                    if len(candidates) > 1 and not names_entrypoint(
                        target.stem, ep_name
                    ):
                        # conformance: ignore[L009] logs caller-invisible context (the rejected candidates) that no HTTP response carries.
                        log.warning(
                            "ConfigMap form chosen alphabetically for entrypoint "
                            "%s: %d generated files are eligible and none is "
                            "named for it, so %s.json was served as the setup "
                            "form (candidates=%s). If that is the wrong file, "
                            "name the form <entrypoint>.json or "
                            "<source>-<entrypoint>.json in the app's contract.",
                            ep_name,
                            len(candidates),
                            target.stem,
                            [c.stem for c in candidates],
                        )

        if target is not None:
            with open(target, encoding="utf-8") as f:
                raw = json.load(f)
            data: dict[str, Any] = {
                "config": orjson.dumps(raw.get("config", raw)).decode()
            }
            # Some configmaps (e.g., connector apps) carry a top-level
            # `defaultConnectorType`; surface it alongside `config` when present.
            default_connector_type = raw.get("defaultConnectorType")
            if default_connector_type is not None:
                data["defaultConnectorType"] = default_connector_type
            configmap = {
                "kind": "ConfigMap",
                "apiVersion": "v1",
                "metadata": {"name": config_map_id},
                "data": data,
            }
            return JSONResponse(
                content=_wrap_response(
                    cast("dict[str, Any]", configmap),
                    message="ConfigMap fetched successfully",
                )
            )

        # conformance: ignore[L009] logs caller-invisible context (the available configmaps) that the generic 404 HTTPException detail does not carry.
        log.warning(
            "ConfigMap not found: requested=%s available=%s",
            config_map_id,
            sorted(available_configmaps),
        )
        raise HTTPException(
            status_code=404, detail=f"ConfigMap '{config_map_id}' not found"
        )

    @app.get("/workflows/v1/configmaps")
    async def list_configmaps() -> JSONResponse:
        # Everything the sibling route serves by exact stem, minus the DAG
        # manifest, which `/workflows/v1/manifest` owns. Deliberately NOT
        # `is_form_configmap`: the credential templates it excludes are ones
        # the UI genuinely fetches by name.
        generated_dir = source.generated_dir()
        seen: set[str] = set()
        configmap_ids: list[str] = []
        if generated_dir.exists():
            for json_file in generated_dir.rglob("*.json"):
                stem = json_file.stem
                if stem == MANIFEST_STEM or stem in seen:
                    continue
                seen.add(stem)
                configmap_ids.append(stem)
        return JSONResponse(
            content=_wrap_response(
                cast("dict[str, Any]", {"configmaps": configmap_ids}),
                message="ConfigMaps listed successfully",
            )
        )

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def _resolve_manifest_placeholders(raw: bytes, origin: str) -> bytes:
        """Substitute the manifest's ``{app_name}`` / ``{deployment_name}`` tokens.

        Delegates to :func:`resolve_manifest_tokens` with the queue the app's
        worker polls, so the served ``task_queue`` is stamped from it rather
        than re-derived and hoped to match (FND-195 / CONNECT-183). An
        ``{app_name}`` token means the committed manifest predates the
        toolkit's bake — a WARNING even when this fills it. With nothing to fill
        it from, the token is served as-is and logged at ERROR: a literal
        ``{app_name}`` in the DAG is greppable, whereas an invented name reads
        as a legitimate queue and hangs the run silently.
        """
        log = source.log()
        resolution = resolve_manifest_tokens(
            raw,
            task_queue=source.task_queue() or None,
            app_name=source.app_name() or source.fallback_app_name or None,
            deployment_fallback=source.deployment_name() or "default",
            from_process_env=source.from_process_env,
        )
        if resolution.unresolved_app_name:
            # conformance: ignore[L009] not a request-boundary handler — the manifest is
            # still served (the token is the diagnostic); this log is the operator's signal.
            log.error(
                "Manifest for %s carries an unresolved {app_name} token and no app "
                "name is available (neither a registered app class nor "
                "ATLAN_APPLICATION_NAME), so the SDK is serving the literal token "
                "rather than inventing a name that would look legitimate and be "
                "polled by nobody. Failure attribution for runs started from this "
                "manifest will be blank, and any task_queue the SDK could not stamp "
                "(stamped queue: %r) targets no worker. Set ATLAN_APPLICATION_NAME "
                "on the deployment, or regenerate the contract so the toolkit bakes "
                "the app name.",
                origin,
                resolution.task_queue,
            )
        elif resolution.had_app_name_token:
            log.warning(
                "Manifest for %s shipped an unbaked {app_name} token; filled with "
                "%r for this response. The committed manifest is stale — bump the "
                "contract toolkit and regenerate contracts, since any writer of "
                "this DAG that does not go through this route gets no such fallback.",
                origin,
                resolution.app_name,
            )
        return resolution.raw

    async def _serve_entrypoint_manifest(
        entrypoint_name: str,
        fe_inputs: str | dict[str, Any] | None,
    ) -> Response:
        """Read <entrypoint>/manifest.json, substitute placeholders, run the
        optional ``compute_manifest`` hook, and return the Response.

        ``fe_inputs`` arrives either as the raw URL-encoded JSON string (GET,
        capped) or an already-parsed dict (POST body, uncapped). It stays
        undecoded until the hook needs it.
        """
        log = source.log()
        generated_dir = source.generated_dir()
        # {entrypoint_name → manifest_path} from glob(), never from user input,
        # so the path that reaches read_bytes() is not tainted by the HTTP param.
        registry: dict[str, Path] = {
            p.parent.name: p for p in generated_dir.glob("*/manifest.json")
        }
        ep_manifest = registry.get(entrypoint_name)
        if ep_manifest is None:
            # Single-entrypoint apps emit the manifest FLAT, with no
            # per-entrypoint subdir. Heracles/AE always sends ?entrypoint= on
            # submit, so without this fallback a flat app 404s and every run
            # submit fails.
            flat_manifest = generated_dir / "manifest.json"
            if flat_manifest.is_file():
                ep_manifest = flat_manifest
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"No manifest found for entrypoint {entrypoint_name!r}",
                )
        raw = _resolve_manifest_placeholders(
            ep_manifest.read_bytes(), f"entrypoint {entrypoint_name!r}"
        )

        # Dynamic-manifest hook: hand the static manifest and the decoded
        # `fe_inputs` to the app's `compute_manifest`, and serve what it
        # returns. Apps without one get the static manifest unchanged.
        compute = source.compute_manifest(entrypoint_name)
        if compute is not None:
            # Decode lazily and only here: an app with no hook has always
            # ignored fe_inputs, so decoding at the route would start 413-ing
            # static apps that work today.
            fe_inputs_decoded = (
                fe_inputs
                if isinstance(fe_inputs, dict)
                else decode_fe_inputs(fe_inputs, source.max_fe_inputs_bytes())
            )
            manifest_dict = orjson.loads(raw)
            try:
                computed = await compute(manifest_dict, fe_inputs_decoded)
            except HTTPException:
                raise
            except Exception:
                # Don't leak the hook's internals (stack/SQL/paths) to the
                # caller — mirror the other handler routes' generic 500.
                # conformance: ignore[L009] boundary handler logs the real exception (exc_info) then raises a sanitized HTTPException `from None`; the log is the only server-side record of the actual failure.
                log.error(
                    "compute_manifest hook failed for entrypoint %r",
                    entrypoint_name,
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500, detail="Internal server error"
                ) from None
            if not isinstance(computed, dict):
                # conformance: ignore[L009] logs caller-invisible context (the actual returned type) that the generic 500 HTTPException detail omits.
                log.error(
                    "compute_manifest for entrypoint %r returned %s, expected dict",
                    entrypoint_name,
                    type(computed).__name__,
                )
                raise HTTPException(status_code=500, detail="Internal server error")
            # Reconcile the hook's output too: it replaces the manifest
            # wholesale, so a token it re-introduced would otherwise be served
            # unreconciled. Idempotent, so a no-op unless the hook added one.
            raw = _resolve_manifest_placeholders(
                orjson.dumps(computed),
                f"entrypoint {entrypoint_name!r} (compute_manifest output)",
            )

        return Response(content=raw, media_type="application/json")

    async def _serve_manifest(
        entrypoint: str | None,
        fe_inputs: str | dict[str, Any] | None,
    ) -> Response:
        """Resolve the entrypoint and serve its manifest (GET and POST alike)."""
        log = source.log()
        generated_dir = source.generated_dir()
        if entrypoint:
            # Reject obviously-malformed names before touching disk.
            if not _ENTRYPOINT_NAME_RE.match(entrypoint):
                raise HTTPException(status_code=400, detail="Invalid entrypoint name")
            return await _serve_entrypoint_manifest(entrypoint, fe_inputs)

        # No entrypoint param: single-entrypoint path.
        if source.programmatic_manifest is not None:
            # A programmatic AppManifest has no contract-toolkit bake behind it,
            # so it gets the same reconciliation as the files.
            raw = _resolve_manifest_placeholders(
                source.programmatic_manifest.model_dump_json().encode(),
                "programmatic manifest",
            )
            return Response(content=raw, media_type="application/json")
        manifest_path = generated_dir / "manifest.json"
        if manifest_path.exists():
            raw = _resolve_manifest_placeholders(
                manifest_path.read_bytes(), "root manifest"
            )
            return Response(content=raw, media_type="application/json")

        # Default-entrypoint fallback: multi-entrypoint apps have no root
        # manifest by design. Try the default first, then every explicit
        # entrypoint alphabetically — the same tie-break the framework uses
        # when several @entrypoints carry no marked default.
        default_ep = _default_entrypoint(
            log,
            "No default entrypoint for app %s; trying explicit-entrypoint "
            "fallback before serving 404",
        )
        candidates: list[str] = []
        if default_ep:
            candidates.append(default_ep)
        candidates.extend(e for e in _explicit_entrypoints() if e not in candidates)
        for cand in candidates:
            try:
                return await _serve_entrypoint_manifest(cand, fe_inputs)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                # This candidate has no manifest file on disk — try the next.

        raise HTTPException(status_code=404, detail="No manifest available")

    @app.get("/workflows/v1/manifest")
    async def get_manifest(
        entrypoint: str | None = None,
        fe_inputs: str | None = None,
    ) -> Response:
        """Serve a manifest with ``fe_inputs`` as a query parameter.

        Retained for the playground, connector integration tests and the
        AE/marketplace probes, which send no ``fe_inputs``. Callers that do
        should use ``POST``: the query string caps the payload (CSA-539).
        """
        return await _serve_manifest(entrypoint, fe_inputs)

    @app.post("/workflows/v1/manifest")
    async def post_manifest(request: Request) -> Response:
        """Serve a manifest with ``fe_inputs`` in the request body.

        Body: ``{"entrypoint": str | null, "fe_inputs": {...}, "user_id": str | null}``
        — all optional. ``{}``, ``null`` and an absent body equal a bare GET.
        ``fe_inputs`` must be a JSON object. ``user_id`` is accepted and ignored.
        """
        raw = await request.body()
        if not raw.strip():
            # Absent/empty body means "no inputs". Callers fall back only on
            # 405, so 400-ing this would hard-fail them.
            body: Any = {}
        else:
            try:
                body = await request.json()
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail=f"Request body is not valid JSON: {exc}"
                ) from exc
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )

        entrypoint = body.get("entrypoint")
        if entrypoint is not None and not isinstance(entrypoint, str):
            raise HTTPException(status_code=400, detail="entrypoint must be a string")

        fe_inputs = body.get("fe_inputs")
        if fe_inputs is None:
            fe_inputs = {}
        if not isinstance(fe_inputs, dict):
            raise HTTPException(
                status_code=400, detail="fe_inputs must be a JSON object"
            )

        return await _serve_manifest(entrypoint, fe_inputs)

    # Backwards-compatibility alias — TACTICAL. Heracles / Automation Engine
    # still call the v2-era unversioned /manifest (BLDX-804).

    @app.get("/manifest", include_in_schema=False)
    async def get_manifest_legacy(
        entrypoint: str | None = None,
        fe_inputs: str | None = None,
    ) -> Response:
        """Unversioned alias for ``GET /workflows/v1/manifest``. Do not add callers."""
        return await get_manifest(entrypoint=entrypoint, fe_inputs=fe_inputs)

    @app.post("/manifest", include_in_schema=False)
    async def post_manifest_legacy(request: Request) -> Response:
        """Unversioned alias for ``POST /workflows/v1/manifest``. Do not add callers."""
        return await post_manifest(request)

    # ------------------------------------------------------------------
    # Input contract
    # ------------------------------------------------------------------

    @app.get("/workflows/v1/input-contract")
    async def get_input_contract(entrypoint: str | None = None) -> Response:
        """Return the input contract (JSON Schema) for an entry point.

        The app's machine-readable input contract: Heracles uses it to validate
        caller inputs and to find credential fields (a ``$ref`` to the
        ``CredentialRef`` / ``AgentCredentialSpec`` def). An explicit
        ``?entrypoint=`` selects it; a single-entry-point app resolves
        automatically; a multi-entry-point app requires the param.
        """
        if entrypoint is not None and not _ENTRYPOINT_NAME_RE.match(entrypoint):
            raise HTTPException(status_code=400, detail="Invalid entrypoint name")
        if source.input_contract is not None:
            contract = source.input_contract(entrypoint)
        else:
            contract = tree_input_contract(
                source.generated_dir(), source.app_package, entrypoint
            )
        return Response(
            content=orjson.dumps(contract.model_json_schema()),
            media_type="application/json",
        )


def generated_dir_of(package_dir: str | os.PathLike[str]) -> Path:
    """Where an app package built by ``gen_app_api.py`` ships its generated files."""
    return Path(package_dir) / "generated"
