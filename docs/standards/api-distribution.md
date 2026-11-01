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
- `release.py` moves the api version and the pin together, and the bump PR
  commits `packages/api/pyproject.toml` with the SDK's own version files.
  `tag-and-publish` publishes the api package, then the SDK, and only then
  creates the `v<version>` tag and GitHub Release, so a failed publish never
  leaves a tag behind. If the api publish fails the SDK is not published; both
  uploads pass `--check-url`, so a rerun skips what already reached PyPI. See
  [Releasing the api package](#releasing-the-api-package).

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
   api-only install, and only when the missing module is one of
   `_WORKER_ONLY_PREFIXES` (the worker modules the guards wrap: the object
   store, the worker's infrastructure, the structured logger, Temporal) or a
   submodule of one. Any other missing module — a listed file, a third-party
   dependency — still raises on the host. A new guard over another worker-only
   module adds that module to `_WORKER_ONLY_PREFIXES`. With the full SDK
   installed a missing module always raises.

`probe_api_wheels.py` then checks the **built** wheels:

- every file under `application_sdk/` ships in exactly one wheel, and every
  tracked source file in one of them;
- a uv upgrade from the last release keeps every listed file, and after a pip
  upgrade the `--force-reinstall` recovery restores a working install;
- an api-only venv imports every listed module within 120 MB RSS, loads nothing
  unlisted, and serves auth, check, metadata and an `AppError` path.

## Adding a surface

Add its module to `[tool.atlan-api].seeds`, then run
`python3 .github/scripts/gen_api_files.py` and
`python3 .github/scripts/check_api_surface.py`. The second names every import
that needs a declared dependency or a guarded fallback.

## Dependencies

- **core** (always installed): fastapi, pydantic, orjson, httpx,
  python-multipart. It covers the handler surface, errors, credential specs,
  the shared routes and `run_in_thread`. Measured at 62 MB on an api-only install.
- **extras**, one per seed group, with the same pins as `atlan-application-sdk`:
  - `sql`: the SQL client base and credential utils;
  - `aws`: the AWS helpers;
  - `pandas`: for SQL result frames.

  - `pyatlan`: only for `ConnectionRef`'s Atlas helpers, which the host never
    calls.

  Measured at 66 MB with every extra installed.
- The structured logger (loguru and OpenTelemetry) is **not** in the api
  distribution. SDK files in the set log through `application_sdk._logging`:
  the SDK's structured logger on the worker, stdlib logging on the host. On the
  host the first such lookup gives the `application_sdk` stdlib logger one
  minimal handler (INFO and above, to stderr) and sets its level to INFO, so
  the shared routes' INFO lines are not dropped by the root logger's default
  WARNING level. It does so only when neither that logger nor the root logger
  has a handler yet; a host that configures logging before importing handler
  code is left alone. If the root logger is given a handler later, the SDK's
  handler stands down so no line is written twice, but the `application_sdk`
  logger stays at INFO: a host that wants it quieter sets that logger's level.
  The worker's logging is unchanged: the full install never takes this path.
- **Handler code does not log.** It reports through its return value or a typed
  `AppError`, and the SDK's shared routes log every outcome with the request id.
  `gen_app_api.py check` fails on logging in a hosted app's handler module
  (the rule conformance **P060** will carry, added in #4414). The `app/` files
  it imports also run on the worker, so they may log, through
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
  left in the handler module (P060), and for an app with an
  `app/<entrypoint>/core.py` `compute_manifest` hook, which the host never runs
  (a manifest computed per request goes in the handler's `compute_manifest`).
- `stage` / `build --out DIR` write the handler's files, its data files and
  `app/generated/` as `<app>_api/*`, with a generated `pyproject.toml` (its
  dependencies, and the `atlan.app_api` and `atlan.app_server` entry points)
  and `__init__.py`. The api requirement is floored at the SDK version the app
  locks in its `uv.lock`, keeping the app's upper bound. A dependency declared
  as a direct reference (`name @ git+https://...@<rev>`, such as a private
  package) ships exactly as declared; the generated `pyproject.toml` sets
  hatchling's `allow-direct-references` only when a dependency, the api
  package included, is one.
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
| `configmap/{id}`, `configmaps`, `manifest` (GET/POST, also the unversioned `/manifest`), `input-contract` | `handler/metadata_routes.py` | Read from the `generated/` folder `gen_app_api.py` ships in the app's package (`*.json` and `_input.py` only). The manifest's task queue is the one the app's worker polls: its `ATLAN_TASK_QUEUE` override when the host's app-env sync carries one (`ATLAN_APPENV__<APP>__ATLAN_TASK_QUEUE`), else `derive_task_queue(app, deployment)` with the deployment read as `ATLAN_APPENV__<APP>__ATLAN_DEPLOYMENT_NAME`, else the host's own `ATLAN_DEPLOYMENT_NAME` (the chart gives the host and every worker the same tenant-wide `deploymentName`; Heracles re-stamps SDR nodes itself). The host never uses its own bare `ATLAN_TASK_QUEUE` or `ATLAN_APPLICATION_NAME` for an app. A manifest computed per request comes from the handler's `compute_manifest`, stamped the same way; `check` refuses a `core.py` `compute_manifest` hook. |
| `config/{id}` (GET/POST), `file` | `handler/object_routes.py` | One S3 bucket from `ATLAN_S3_BUCKET` / `ATLAN_S3_REGION` (the `aws` extra, ambient AWS identity). On GCP `ATLAN_GCP_BUCKET` / `ATLAN_GCP_REGION` instead, through GCS's S3-compatible endpoint with `ATLAN_GCP_HMAC_ACCESS_KEY` / `ATLAN_GCP_HMAC_SECRET`, sending checksums only where an API requires them (GCS rejects boto3's default CRC32). A config is keyed `persistent-artifacts/apps/<app>/<type>/<id>/config.json` with the hosted app's name, the key its worker reads. An upload's `prefix` must be relative path segments with no `..`, and on the host every upload lands under the app's own `persistent-artifacts/apps/<app>/uploads/`. 503 without either bucket, as on an Azure Blob tenant (not served yet). The host's manifest sets each of these as an alias of the chart's name (`ATLAN_GCP_BUCKET: "$(GCP_BUCKET)"`); an alias the chart leaves unresolved on another cloud (`$(...)` text) counts as unset. |

The worker registers the same modules with its own lookups (the app registry,
its Dapr-configured store), and `test_metadata_route_parity.py` /
`test_object_routes_host.py` pin that the two answer alike.

**The split changes no behaviour, except two deliberate fixes: secrets are
redacted from response bodies; a malformed body is a 422.** These modules
were moved out of `handler/service.py` (and `credentials/utils.py`) as they
were, so the shared routes answer exactly as the worker's did before the split,
on the worker and on the host: the same request contract (422 for a body that
fails its typed contract), the same v2 credential normalization (only
`credentials` is replaced, and `/start` strips only that key), the same log
redaction (`errors/base.py` and `errors/wire.py` are
unchanged) and the same logger names (`application_sdk.handler.service`,
`application_sdk.credentials.utils`), so log queries and alerts keep matching.
The first fix: every response-body text field that can carry exception or
handler text (an error's `detail`, the envelope `message`, `AuthOutput.message`, each
preflight check's and the verdict's message, a warmup's `source_state`) goes
through `redact_secrets` as the body is built, so an
`AuthError("... postgres://u:<pw>@h/db")` answers 401 with
`postgres://***@h/db` where it used to carry the password. A malformed
`fe_inputs` answers a fixed `fe_inputs is not valid JSON`, without the parser's
text. Logs and models are unchanged; `test_response_redaction.py` pins the body
on both surfaces. The second: `/workflows/v1/auth`, `/check`, `/warmup` and
`/metadata` read the body with `read_json_object`, so one that is not valid
JSON (`{bad`) or is valid JSON but not an object (`[1]`, `"string"`) answers
the contract's own 422, `{"success": false, "message": "Invalid request: body",
"detail": []}`, never echoing the body, where it used to escape as a bare 500
(the log line is unchanged). `test_request_contract.py` pins it on both
surfaces.

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
- **One identity** (the pod's AWS identity, or on GCP the tenant's HMAC key)
  writes the shared bucket for every app; uploads are
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

## Releasing the api package

- **Publishing.** `atlan-application-sdk-api` is published with PyPI trusted
  publishing (OIDC), not a token. The trusted publisher on PyPI is bound to the
  repository `atlanhq/application-sdk`, the workflow file
  `.github/workflows/tag-and-publish.yaml`, with any environment allowed.
  Renaming that file breaks api publishing until the PyPI entry is updated to
  match.
- **Yanking.** The two packages are one release. Yanking an SDK version means
  yanking `atlan-application-sdk-api` at the same version too, and the reverse:
  an SDK left installable pins an api that is not, and an api left installable
  advertises a release that was withdrawn.
- **Release candidates** need nothing extra: an `rc` SDK pins the `rc` api of
  the same version, and both are published by the same run.
