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
- `atlan-application-sdk` also ships every listed file, byte for byte, and pins
  `atlan-application-sdk-api==<its own version>`. The overlap is deliberate.
  With disjoint wheels, `pip install -U` from a release older than the split
  deletes the files the api wheel has just written, and nothing reports it.
- `release.py` moves the api version and the pin together, and
  `tag-and-publish` publishes the api package first.

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

- identical bytes in both wheels;
- a pip upgrade from the last release keeps every listed file;
- an api-only venv imports every listed module within 120 MB RSS, loads nothing
  unlisted, and serves auth, check, metadata and an `AppError` path without
  leaking a DSN.

## Adding a surface

Add its module to `[tool.atlan-api].seeds`, then run
`python3 .github/scripts/gen_api_files.py` and
`python3 .github/scripts/check_api_surface.py`. The second names every import
that needs a declared dependency or a guarded fallback.

## Dependencies

- **core** (always installed): fastapi, pydantic, orjson. It covers the handler
  surface, errors, credential specs, the shared routes and `run_in_thread`.
  Measured at 57 MB on an api-only install.
- **extras**, one per seed group, with the same pins as `atlan-application-sdk`:
  - `sql`: the SQL client base and credential utils;
  - `aws`: the AWS helpers;
  - `pandas`: for SQL result frames.

  Measured at 60 MB with every extra installed.
- The structured logger (loguru and OpenTelemetry) is **not** in the api
  distribution. SDK files in the set log through `application_sdk._logging`:
  the SDK's structured logger on the worker, stdlib logging on the host.
- **Handler code does not log.** It reports through its return value or a typed
  `AppError`, and the SDK's shared routes log every outcome with the request id.
  Conformance rule **P054** blocks logging in a hosted app's handler code.

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

- `fix` makes imports between the handler's `app/` files relative, moves
  `run_in_thread` to `application_sdk.common.concurrency` (its usual path loads
  Temporal), and deletes logging statements (P054). Review what it removed: a
  log line was sometimes the only report of a failure the handler should raise
  as a typed `AppError`.
- `check` runs in CI and fails on an absolute `from app` import in those files.
- `build --out DIR` stages the files as `<app>_api/*`, generates
  `pyproject.toml` (deps and the `atlan.app_api` entry point) and
  `__init__.py`, then builds the wheel the host installs.

The tests-reusable `api-member` job runs `check`, builds the wheel, installs it
alone, and mounts it with `application_sdk.handler.asgi.build_asgi_app`, exactly
as the host does. The reference is atlan-mysql-app#778; see the
`api-server-consolidation-migration` skill.

## App routes

An app that serves endpoints of its own returns FastAPI routers from
`Handler.routers()`. `register_handler_routes` serves them after the SDK's
routes, on the worker and on the host alike, so the host needs no knowledge of
any app. A router that redefines an SDK path is refused at startup. Keep router
code in the handler's own files (imported relatively), so it ships with the
handler.

## Versions

| Artifact | Version | Built and published by |
|---|---|---|
| `atlan-application-sdk` | the SDK release, N | `tag-and-publish` |
| `atlan-application-sdk-api` | always N too (same commit) | `tag-and-publish`, published first |
| `<app>_api` (for example `atlan_mysql_api`) | the app's release, Y (its root `pyproject.toml` version) | `gen_app_api.py build` in the app's release |

- The SDK pins `atlan-application-sdk-api==N`, so the files the worker gets from
  both packages are byte-identical.
- An app's wheel requires `atlan-application-sdk-api[<extras>]` with the range
  the app already declares for `atlan-application-sdk` (for example
  `>=3.40,<4`). Before an SDK release it carries the app's git pin instead.
- The host pins each hosted app's wheel version, resolves one api version that
  satisfies every app's range, and fails its own lock in CI when it can't.
