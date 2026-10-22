#!/usr/bin/env python3
"""Mount an app's api member the way the consolidated host does, and exercise it.

Run with the interpreter of a venv that has ONLY the api member installed (its
own declared dependencies — ``atlan-application-sdk-api``, never the full
``atlan-application-sdk``)::

    uv venv .venv-api && uv pip install --python .venv-api ./api
    .venv-api/bin/python probe_app_api_member.py --name mysql --package atlan_mysql_api

Fails when:

* the member pulls in the full ``atlan-application-sdk`` (the host installs only
  the api distribution; the member must depend on ``atlan-application-sdk-api``).
  An import of worker-only code then fails on its own, at import;
* the entry-point object is not a ``Handler``;
* ``build_asgi_app`` cannot build it, or the app's ``state.app_name`` is not the
  entry-point name (the host routes on it);
* the host matrix fails: ``/health`` is not 200, a malformed body on
  ``/workflows/v1/auth`` is not 422, or an empty-credentials auth call answers a
  bare 500 instead of a JSON envelope;
* ``/workflows/v1/check`` or ``/workflows/v1/metadata``, called with empty
  credentials, reaches a worker-only import. Those calls are expected to fail
  (there is no source), so the probe judges them by what the handler raised,
  not by the status: an ``ImportError`` (``ModuleNotFoundError`` included) is a
  code path that only breaks on the host;
* for an app that ships generated contract files, the metadata routes fail:
  ``/workflows/v1/configmaps`` is not 200, ``POST /manifest`` (what Heracles
  calls on every run submit) is not 200 JSON or still carries an unresolved
  ``{app_name}`` / ``{deployment_name}`` token, or ``/workflows/v1/input-contract``
  answers a 5xx. When the handler overrides ``compute_manifest``, the manifest
  of every entry point in its generated tree is checked the same way, and an
  ``ImportError`` it raises fails the probe;
* the config route, with no object store configured, does not answer 503.

common-app-server's image build runs the same probe, so an app PR that would
break the host fails in the app's CI first.
"""

from __future__ import annotations

import argparse
import functools
import importlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Handler methods the host serves that the probe calls with empty credentials.
_PROBED = {
    "preflight_check": "/workflows/v1/check",
    "fetch_metadata": "/workflows/v1/metadata",
}

#: Handler methods whose ImportError the probe records: those above, and
#: ``compute_manifest``, which the manifest routes run.
_WATCHED = (*_PROBED, "compute_manifest")


@dataclass
class Result:
    problems: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.problems.append(message)


def check_api_only(full_sdk_installed: bool, result: Result) -> None:
    """The member's venv must hold the api distribution, not the full SDK."""
    if full_sdk_installed:
        result.fail(
            "the api member installs atlan-application-sdk (the worker SDK); "
            "depend on atlan-application-sdk-api, which the host installs"
        )


def check_response(
    label: str,
    status: int,
    body: object,
    result: Result,
    *,
    want: int | None = None,
    not_500_json: bool = False,
) -> None:
    if want is not None and status != want:
        result.fail(f"{label}: expected HTTP {want}, got {status}")
    if not_500_json and (status == 500 and not isinstance(body, dict)):
        result.fail(f"{label}: answered a bare 500 instead of a JSON envelope")


def record_import_failures(handler: Any, seen: dict[str, ImportError]) -> None:
    """Wrap the watched handler methods so an ImportError they raise is recorded.

    The error still propagates, so the route answers exactly as it would on
    the host.
    """
    for method in _WATCHED:
        original = getattr(handler, method)

        @functools.wraps(original)
        async def wrapped(*args: Any, _m: str = method, _f: Any = original) -> Any:
            try:
                return await _f(*args)
            except ImportError as exc:
                seen.setdefault(_m, exc)
                raise

        setattr(handler, method, wrapped)


def ships_contracts(generated: Path) -> bool:
    """Whether the app's source tree has contract-toolkit output to serve."""
    return generated.is_dir() and any(generated.glob("*.json"))


def check_manifest(client: Any, result: Result, label: str, body: object) -> None:
    """``POST /manifest`` with *body* answers 200 with every token resolved."""
    manifest = client.post("/manifest", json=body)
    check_response(label, manifest.status_code, None, result, want=200)
    if manifest.status_code == 200:
        text = manifest.text
        for token in ("{app_name}", "{deployment_name}"):
            if token in text:
                result.fail(
                    f"{label}: the served manifest still carries {token}; "
                    "a run submitted from it would target no worker"
                )


def check_computed_manifests(
    client: Any, result: Result, entrypoints: list[str]
) -> None:
    """Every entry point's manifest, for a handler that computes them."""
    for entrypoint in entrypoints:
        check_manifest(
            client,
            result,
            f"POST /manifest (entrypoint {entrypoint})",
            {"entrypoint": entrypoint, "fe_inputs": {}},
        )


def check_metadata_routes(
    client: Any, result: Result, *, expects_contracts: bool
) -> None:
    """The routes Heracles serves setup forms and run submits from.

    A 404 on ``/configmaps`` is a pass only for an app with no generated
    contract files. When its source ships them (``expects_contracts``), the 404
    means they are missing from the api member wheel, and the host would serve
    no setup form, manifest or input contract.
    """
    listing = client.get("/workflows/v1/configmaps")
    if listing.status_code == 404:
        if expects_contracts:
            result.fail(
                "GET /workflows/v1/configmaps: 404, but the app ships generated "
                "contract files (app/generated/*.json); they are missing from "
                "the api member wheel"
            )
        return
    check_response(
        "GET /workflows/v1/configmaps", listing.status_code, None, result, want=200
    )
    check_manifest(client, result, "POST /manifest", {})
    contract = client.get("/workflows/v1/input-contract")
    if contract.status_code >= 500:
        result.fail(
            f"GET /workflows/v1/input-contract: answered {contract.status_code}"
        )


def run(
    name: str, package: str, obj: str, *, expects_contracts: bool = False
) -> Result:
    result = Result()
    module = importlib.import_module(package)
    check_api_only(importlib.util.find_spec("application_sdk.main") is not None, result)

    from application_sdk.handler import Handler  # noqa: PLC0415
    from application_sdk.handler.asgi import build_asgi_app  # noqa: PLC0415
    from application_sdk.handler.metadata_routes import (  # noqa: PLC0415
        generated_dir_of,
        tree_entrypoints,
    )

    handler = getattr(module, obj, None)
    if callable(handler) and not isinstance(handler, Handler):
        handler = handler()
    if not isinstance(handler, Handler):
        result.fail(
            f"{package}:{obj} is {type(handler).__name__}, not an application_sdk Handler"
        )
        return result

    import_failures: dict[str, ImportError] = {}
    record_import_failures(handler, import_failures)
    factory = getattr(module, "get_asgi_app", None)
    if callable(factory):
        # What the host calls (entry point atlan.app_server); it serves the same
        # handler instance, so the import-failure wrapping above applies.
        app = factory()
    else:
        app = build_asgi_app(handler, app_name=name, app_package=package)
    if getattr(app.state, "app_name", None) != name:
        result.fail(
            f"app.state.app_name is {getattr(app.state, 'app_name', None)!r}, expected {name!r}"
        )

    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    health = client.get("/health")
    check_response("GET /health", health.status_code, None, result, want=200)
    bad = client.post(
        "/workflows/v1/auth",
        content=b"[1, 2]",
        headers={"content-type": "application/json"},
    )
    check_response(
        "POST /workflows/v1/auth (non-object body)",
        bad.status_code,
        None,
        result,
        want=422,
    )
    auth = client.post("/workflows/v1/auth", json={"credentials": []})
    try:
        body: object = auth.json()
    except ValueError:
        body = None
    check_response(
        "POST /workflows/v1/auth (empty credentials)",
        auth.status_code,
        body,
        result,
        not_500_json=True,
    )
    check_metadata_routes(client, result, expects_contracts=expects_contracts)
    if type(handler).compute_manifest is not Handler.compute_manifest:
        generated = generated_dir_of(Path(module.__file__ or ".").parent)
        check_computed_manifests(client, result, tree_entrypoints(generated))
        if "compute_manifest" in import_failures:
            exc = import_failures["compute_manifest"]
            result.fail(
                "POST /manifest: compute_manifest reached a worker-only import "
                f"({type(exc).__name__}: {exc})"
            )
    config = client.get("/workflows/v1/config/probe-guid?type=credentials")
    check_response(
        "GET /workflows/v1/config (no object store)",
        config.status_code,
        None,
        result,
        want=503,
    )
    for method, path in _PROBED.items():
        response = client.post(path, json={"credentials": [], "metadata": {}})
        try:
            body = response.json()
        except ValueError:
            body = None
        label = f"POST {path} (empty credentials)"
        check_response(label, response.status_code, body, result, not_500_json=True)
        if method in import_failures:
            exc = import_failures[method]
            result.fail(
                f"{label}: reached a worker-only import "
                f"({type(exc).__name__}: {exc})"
            )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--object", default="handler")
    parser.add_argument(
        "--generated",
        type=Path,
        default=Path("app/generated"),
        help="the app's contract-toolkit output in its source tree",
    )
    args = parser.parse_args(argv)
    result = run(
        args.name,
        args.package,
        args.object,
        expects_contracts=ships_contracts(args.generated),
    )
    for line in result.problems:
        print(f"::error::api member: {line}", file=sys.stderr)
    if not result.problems:
        print(
            f"api member {args.package} mounts as {args.name!r} and passes the host matrix"
        )
    return 1 if result.problems else 0


if __name__ == "__main__":
    sys.exit(main())
