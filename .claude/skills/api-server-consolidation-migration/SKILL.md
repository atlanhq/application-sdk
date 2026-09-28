---
name: api-server-consolidation-migration
description: >
  Move an Atlan app onto the shared common-app-server (API Server Consolidation,
  ARUN-942) the supported way: move the app's ONE handler, its failures and its
  client out of app/ into a uv workspace member built on atlan-application-sdk-api,
  change imports, declare an atlan.app_api entry point, and let the host build the
  ASGI app. No second handler, no serving-package copy, no parity suite. Use when
  onboarding an app to the consolidated host, migrating an app off a hand-written
  server/<app>_server package, or when a hosted app 503s, returns app_not_hosted,
  or fails to mount.
---

# Moving an app onto the consolidated API server

The host (`atlanhq/common-app-server`) serves many apps' handler HTTP surface in
one always-on process per tenant. **The app keeps exactly one handler.** The
worker's preflight gate, the SDR workflows and the host all run the same class.

Never write a second handler, a `server/<app>_server` package, or a parity suite.
Those were the previous approach; they drift (a hosted copy returned PARTIAL where
the worker raised, and an empty list where it raised a typed error).

## What lives where

| Package | Owns | Imported by |
|---|---|---|
| `atlan-application-sdk-api` (`application_sdk_api`) | errors, handler contracts, `Handler`, `HandlerContext`, the auth/check/metadata routes, `build_asgi_app` | the app's api member, the host, and `application_sdk` (which re-exports it) |
| `atlan-application-sdk` (`application_sdk`) | worker, Temporal, storage, OTEL; depends on the api package at its own version | the app's worker code |
| `atlan-<app>-api` (`api/atlan_<app>_api/`) | the app's handler, failures, the client the handler needs | the worker **and** the host |

Rules (enforced by conformance):

- Handler code imports the handler surface from `application_sdk_api.handler`.
  `application_sdk.handler*` still works but warns (removal in v4.0).
- `application_sdk.errors` is **not** deprecated. Worker code keeps using it.
  Inside the api member, import errors from `application_sdk_api.errors`.
- The api member imports only `application_sdk_api`, itself and drivers — never
  `application_sdk.*` or `app.*`, and no module-level `os.environ` reads.
- The app root never declares `atlan-application-sdk-api`; it comes from the SDK,
  pinned exactly. The api member declares `atlan-application-sdk-api>=X,<N+1`.

## Procedure

1. **Create the workspace member.**

   ```text
   api/
     pyproject.toml          # name = "atlan-<app>-api"
     atlan_<app>_api/
       __init__.py           # handler = <App>AppHandler()
       handler.py            # git mv from app/handler.py
       failures.py           # git mv from app/failures.py
       client.py             # the connection code the handler needs (moved, not copied)
       sql/*.sql             # git mv from app/sql/
   ```

   `api/pyproject.toml`:

   ```toml
   [project]
   name = "atlan-<app>-api"
   dependencies = ["atlan-application-sdk-api[sql,aws]>=3.41,<4", "<driver>"]

   [project.entry-points."atlan.app_api"]
   <app> = "atlan_<app>_api:handler"   # MUST equal the app's Service name / app_name

   [tool.hatch.build.targets.wheel.force-include]
   "../app/generated" = "atlan_<app>_api/generated"   # configmap JSON the host serves
   ```

   Root `pyproject.toml`: add `atlan-<app>-api` to dependencies with
   `[tool.uv.sources] atlan-<app>-api = { workspace = true }` and
   `[tool.uv.workspace] members = ["api"]`.

2. **Change imports, nothing else.** In the moved files:
   `application_sdk.handler*` → `application_sdk_api.handler*`,
   `application_sdk.errors*` → `application_sdk_api.errors*`,
   `application_sdk.observability.logger_adaptor` →
   `application_sdk_api.observability.logger_adaptor`, `app.failures` →
   `atlan_<app>_api.failures`. In `app/<app>.py` import the handler class from
   `atlan_<app>_api.handler` so SDK handler discovery still finds it. Fix test
   imports and `mock.patch` targets that named the old modules.

3. **Resolve worker-only imports in moved files.** The thin-closure rule lists
   every import the api member may not make. For each: use the api package's
   equivalent if it exists; otherwise leave that code in `app/` (the worker
   subclasses or wraps the moved piece). Never copy logic into both places — if
   something has to be duplicated until the api package grows an equivalent,
   list it in the PR body.

4. **Re-lock inside a container** (a managed uv config on developer machines
   rewrites indexes): `docker run --rm -v "$PWD":/w -w /w python:3.11-bookworm
   bash -c "pip install -q uv==0.12.17 && uv lock"`. The diff must stay confined to
   the changed packages. Then `uv sync --locked --no-install-project --no-dev` in
   the same container.

5. **Test.** The reusable test workflow runs the api member alone in a thin venv,
   mounts it with `build_asgi_app`, and drives auth/check/metadata against the
   app's integration source. Locally:

   ```python
   from fastapi.testclient import TestClient
   from application_sdk_api import build_asgi_app
   import atlan_<app>_api

   client = TestClient(build_asgi_app(atlan_<app>_api.handler, app_name="<app>",
                                      app_package="atlan_<app>_api"))
   assert client.post("/workflows/v1/auth", json={"credentials": []}).status_code != 500
   ```

6. **Host onboarding.** The host discovers `atlan.app_api` entry points and calls
   `build_asgi_app(handler, app_name=<entry-point name>, app_package=...,
   generated_dir=<package>/generated)`. Add the app to `hosted-apps.toml` in
   `common-app-server` (published main build + api wheel hash). Routing is
   unchanged: set `deploy.routeToCommonAPIServer: true` in `atlan.yaml`.

## When a hosted app fails

| Symptom | Cause |
|---|---|
| `503 app_not_hosted` | the Host label matches no mounted app: entry-point name ≠ Service name, or the app is not in the host image |
| app ejected to a 503 stub | `build_asgi_app` or the handler import raised at mount; the host log names the exception |
| blank setup form behind a 200 | the api wheel does not ship `generated/`; check the `force-include` |
| `/start` 503 | the host has no Temporal client configured (`ATLAN_TEMPORAL_HOST`) |
| `get_secret` raises `SecretStoreNotConfiguredError` | the host has no secret store; read the request's credentials with `get_credential` |
