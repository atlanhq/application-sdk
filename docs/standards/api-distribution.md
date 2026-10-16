# The api distribution (`atlan-application-sdk-api`)

The consolidated API host serves every hosted app's handler (auth / check /
metadata) from one process. It cannot install `atlan-application-sdk` per app:
the worker SDK carries Temporal, Dapr, the object store and the rest of the
worker runtime. So a **listed subset of `application_sdk/`** is also published
on its own, as `atlan-application-sdk-api`.

There is one source tree and one import path. `from application_sdk.handler
import Handler` is the same file whether the full SDK or only the api
distribution is installed. Nothing is copied, shimmed or deprecated.

## How it is built

- `[tool.atlan-api].seeds` in `packages/api/pyproject.toml` names the modules
  the host needs. `packages/api/api-files.txt`, the files the api wheel ships,
  is **generated** from them by `.github/scripts/gen_api_files.py` (their import
  closure) and committed; CI fails when it is stale.
- `packages/api/hatch_build.py` copies those files into the sdist and the wheel.
  An editable build copies nothing, so the SDK's dev env keeps importing the
  source tree.
- **The two wheels are disjoint.** The listed files ship only in the api wheel;
  `atlan-application-sdk` excludes them (a generated `exclude` block in the root
  `pyproject.toml`, written by `gen_api_files.py` from the same list) and gets
  them through its `atlan-application-sdk-api==<its own version>` dependency.
  Two wheels owning one file would mean uninstalling either deletes it while
  the other still reports installed.
- **Upgrading from a release older than the split:** uv (the fleet image and the
  host) keeps every file. pip's uninstall of the old release deletes the files
  the api wheel just wrote; recover with
  `pip install --force-reinstall atlan-application-sdk-api`.
- `release.py` moves the api version and the pin together, and
  `tag-and-publish` publishes the api package first. The release fails before
  tagging when the api package's PyPI token is unset, so neither is ever
  published alone.

## Rules for a listed file

`check_api_surface.py` enforces these in CI (the `API Suite Tests` gate job):

1. It imports only other listed `application_sdk` modules at module level.
2. Its third-party module-level imports are dependencies of `packages/api`.
   Function-level imports of an **extra** (`sql`, `pandas`, `aws`) are fine.
   A handler that needs one declares the extra, as it does on the worker.
3. A function-level import of an unlisted module is wrapped like this:

   ```python
   try:
       from application_sdk.storage.ops import download_file
   except ModuleNotFoundError as exc:
       if not worker_only_missing(exc):
           raise
       raise ObjectStoreNotConfiguredError() from exc  # the stated fallback
   ```

   `worker_only_missing` (`application_sdk._install`) is true only on an
   api-only install. With the full SDK installed a missing module still raises.

`probe_api_wheels.py` then checks the **built** wheels:

- every file under `application_sdk/` ships in exactly one wheel, and every
  tracked source file in one of them;
- a uv upgrade from the last release keeps every listed file, and after a pip
  upgrade the `--force-reinstall` recovery restores a working install;
- an api-only venv imports every listed module within 120 MB RSS, loads nothing
  unlisted, and serves auth, check, metadata and an `AppError` path without
  leaking a DSN.

## Adding a surface

Add its module to `[tool.atlan-api].seeds`, then run
`python3 .github/scripts/gen_api_files.py` and
`python3 .github/scripts/check_api_surface.py`. The second names every import
that needs a declared dependency or a guarded fallback.

## Dependencies

- **core** (always installed): fastapi, pydantic, orjson, python-multipart. It
  covers the handler surface, errors, credential specs, the shared routes and
  `run_in_thread`. Measured at 62 MB on an api-only install.
- **extras**, one per seed group, with the same pins as `atlan-application-sdk`:
  - `sql`: the SQL client base and credential utils;
  - `aws`: the AWS helpers;
  - `pandas`: for SQL result frames.

  - `pyatlan`: only for `ConnectionRef`'s Atlas helpers, which the host never
    calls.

  Measured at 66 MB with every extra installed.
- The structured logger (loguru and OpenTelemetry) is **not** in the api
  distribution. SDK files in the set log through `application_sdk._logging`:
  the SDK's structured logger on the worker, stdlib logging on the host.
- **Handler code does not log.** It reports through its return value or a typed
  `AppError`, and the SDK's shared routes log every outcome with the request id.
  Conformance rule **P060** blocks logging in a hosted app's handler module. The
  `app/` files it imports also run on the worker, so they may log, through
  `application_sdk.common.logger`.

## App side

The handler stays in `app/`. The app commits one config block and no
packaging:

```toml
[tool.atlan-app-api]
handler = "app.handler:MySQLAppHandler"
data = ["app/sql/test_authentication.sql"]
dependencies = ["aiomysql>=0.3.0"]
extras = ["sql", "aws"]
```

`.github/scripts/gen_app_api.py` does the rest:

- `fix` makes imports between the handler's `app/` files relative, and moves
  two imports whose usual path is worker-only: `run_in_thread` to
  `application_sdk.common.concurrency` (its usual path loads Temporal) and
  `get_logger` to `application_sdk.common.logger`. It deletes logging
  statements from the handler module only (P060); the other files also run on
  the worker and keep their logs. It **refuses, changing nothing,** where a
  deletion would be unsafe: a log call sharing a line with other code, a
  logger import mixed with other names (`get_logger`, or `getLogger` /
  `logger` from `logging` / `loguru`), a logger stored on an attribute
  (`self.logger`), or an `except` block that would be left as `pass` (raise a
  typed `AppError` there instead). It needs ruff on PATH and checks its own
  edits with it: on any unused import, unused variable or undefined name it
  introduced (ruff F401/F841/F821) it restores every file and fails. It prints
  each deletion as `file:line`. Needs Python 3.11+.
- `check` runs in CI and fails, in those files, on an absolute `from app`
  import, a worker-only import `fix` would move, or a read of
  `APPLICATION_NAME` / `ATLAN_APPLICATION_NAME`. On the host that names the
  host itself; a handler uses `self.context.app_name`. It also fails on logging
  left in the handler module (P060), and for an app with a `compute_manifest`
  hook, which the host cannot serve.
- `stage` / `build --out DIR` write the handler's files, its data files and
  `app/generated/` as `<app>_api/*`, with a generated `pyproject.toml` (its
  dependencies, and the `atlan.app_api` and `atlan.app_server` entry points)
  and `__init__.py`. The api requirement is floored at the SDK version the app
  locks in its `uv.lock`, keeping the app's upper bound.
- `hash` prints the content hash of that package; a worker-only change leaves
  it unchanged.

The tests-reusable `api-member` job runs `check`, builds the wheel, installs it
alone, and mounts it with `application_sdk.handler.asgi.build_asgi_app`, exactly
as the host does. The reference is atlan-mysql-app#778; see the
`api-server-consolidation-migration` skill.

## Routes the host serves

Once an app sets `routeToCommonAPIServer`, every Heracles call for it lands on
the host, so `build_asgi_app` serves the same routes the worker's handler
service does for it, from one shared implementation each:

| Routes | Module | On the host |
|---|---|---|
| `auth`, `check`, `warmup`, `metadata` | `handler/routes.py` | The app's handler. |
| `configmap/{id}`, `configmaps`, `manifest` (GET/POST, also the unversioned `/manifest`), `input-contract` | `handler/metadata_routes.py` | Read from the `generated/` folder `gen_app_api.py` ships in the app's package (`*.json` and `_input.py` only). The manifest's task queue is the one the app's worker polls: its `ATLAN_TASK_QUEUE` override when the host's app-env sync carries one (`ATLAN_APPENV__<APP>__ATLAN_TASK_QUEUE`), else `derive_task_queue(app, deployment)` with the deployment read as `ATLAN_APPENV__<APP>__ATLAN_DEPLOYMENT_NAME` only. The host never reads its own bare `ATLAN_*` keys for an app; with neither per-app key it stamps no queue and logs an ERROR. Static manifests only: `check` refuses an app with a `compute_manifest` hook. |
| `config/{id}` (GET/POST), `file` | `handler/object_routes.py` | One S3 bucket from `S3_BUCKET` / `S3_REGION` (the `aws` extra, ambient AWS identity). A config is keyed `persistent-artifacts/apps/<app>/<type>/<id>/config.json` with the hosted app's name, the key its worker reads. An upload's `prefix` must be relative path segments with no `..`, and on the host every upload lands under the app's own `persistent-artifacts/apps/<app>/uploads/`. 503 without `S3_BUCKET`. |

The worker registers the same modules with its own lookups (the app registry,
its Dapr-configured store), and `test_metadata_route_parity.py` /
`test_object_routes_host.py` pin that the two answer alike.

## Trust model on the host

The host runs many apps in one process. That is only acceptable because every
hosted app is **trusted first-party code**:

- **No secret isolation between apps.** Every hosted app's environment
  (`ATLAN_APPENV__<APP>__*`, the host's own env) is in one `os.environ`,
  readable by every app's code.
- **The host trusts the request's `Host` / `X-Original-Host` to pick the app**
  and does not authenticate the caller; it relies on only Heracles and the
  ingress being able to reach the host pod (a NetworkPolicy, owned by App
  Runtime).
- **Shared capacity.** Each app has its own `run_in_thread` / `submit_in_thread`
  pool, but the event loop, FastAPI's thread pool and the host's request
  concurrency limit are shared.
- **One AWS identity** writes the shared bucket for every app; uploads are
  contained to each app's namespace by the shared route, and config keys use
  the mounted app's name, never request input.

## App routes

An app that serves endpoints of its own returns FastAPI routers from
`Handler.routers()`. `register_handler_routes` serves them after the SDK's
routes, on the worker and on the host alike, so the host needs no knowledge of
any app. A router that redefines an SDK path is refused at startup. Keep router
code in the handler's own files (imported relatively), so it ships with the
handler.

## Versions and how the host gets an app

| Artifact | Version | Built and published by |
|---|---|---|
| `atlan-application-sdk` | the SDK release, N | `tag-and-publish` |
| `atlan-application-sdk-api` | always N too (same commit) | `tag-and-publish`, published first |
| `<app>_api` (for example `atlan_mysql_api`) | the app's release | staged from source by the host's image build |

- The SDK depends on `atlan-application-sdk-api==N` and ships none of its files.
- An app's package requires `atlan-application-sdk-api[<extras>]`, floored at
  the SDK version the app locks and capped by the app's declared upper bound.
  Before an SDK release it carries the app's git pin instead.
- **The host pulls released versions; nothing pushes to it.** common-app-server
  pins each hosted app, and the SDK whose `gen_app_api.py` stages them, by
  **release tag plus commit SHA**, and Renovate opens the bump PRs. The SDK's
  release workflow knows nothing about the host. The host resolves one api
  version for every app and fails its own lock in CI when it can't, and probes
  every staged app in its locked environment before a pin moves.
