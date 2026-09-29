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
  bare 500 instead of a JSON envelope.

The same checks gate the host's own pin bump, so an app PR that would break the
host fails in the app's CI first.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
from dataclasses import dataclass, field


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


def run(name: str, package: str, obj: str) -> Result:
    result = Result()
    module = importlib.import_module(package)
    check_api_only(importlib.util.find_spec("application_sdk.main") is not None, result)

    from application_sdk.handler import Handler  # noqa: PLC0415
    from application_sdk.handler.asgi import build_asgi_app  # noqa: PLC0415

    handler = getattr(module, obj, None)
    if callable(handler) and not isinstance(handler, Handler):
        handler = handler()
    if not isinstance(handler, Handler):
        result.fail(
            f"{package}:{obj} is {type(handler).__name__}, not an application_sdk Handler"
        )
        return result

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
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--object", default="handler")
    args = parser.parse_args(argv)
    result = run(args.name, args.package, args.object)
    for line in result.problems:
        print(f"::error::api member: {line}", file=sys.stderr)
    if not result.problems:
        print(
            f"api member {args.package} mounts as {args.name!r} and passes the host matrix"
        )
    return 1 if result.problems else 0


if __name__ == "__main__":
    sys.exit(main())
