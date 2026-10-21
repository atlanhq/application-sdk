# Troubleshooting

Read from the `api-server-consolidation-migration` skill. Fix the app first:
every row below is remedied in the app, per the skill's stance, unless it says
otherwise.

| Symptom | Cause | Fix |
|---|---|---|
| Host `503` with `reason: import_error` | Importing the staged package failed: a worker-only import at module level, or a dependency missing from `dependencies` / `extras` | Run the local probe. Use the api-shipped path (`application_sdk.common.concurrency`, `application_sdk.common.logger`), or move the code off the worker-only module. Adding a module to the api package is an SDK change: only under the skill's decision rule (openapi, mysql or metabase would need it too). |
| `503 factory_error` | `get_asgi_app()` raised, e.g. the handler's `__init__` touched config or the network | Keep handler construction side-effect free |
| `503 name_mismatch` / `missing_app_name` | Entry-point name ≠ `app.state.app_name`: a hand-edited staged package or a wrong `name =` | Fix `name =`. Never edit the generated `__init__.py` |
| `503 invalid_app`, `lifespan_error`, `lifespan_timeout` | The factory returned a non-ASGI object, or a lifespan raised or hung | The canonical shape has no lifespan; remove it. Startup work is out of scope: escalate |
| `503 app_not_hosted` | Flag flipped before the host image mounted the app, or Host label ≠ entry-point name | Image before flag (step 8); routing key (step 2) |
| `/check` or `/metadata` 500 with `ModuleNotFoundError` | A deferred worker-only import on a request path (Temporal, Dapr, `application_sdk.storage`, ...) | The probe catches it; move the code to an api path |
| `SecretStoreNotConfiguredError` / `ObjectStoreNotConfiguredError` | The host has no secret store and no object store | Use the request's credentials. Config and file need `ATLAN_S3_BUCKET` / `ATLAN_S3_REGION` and an S3 role on the host pod |
| Another app is ejected once yours is on the host | That app treats `import application_sdk` succeeding as "I am on the worker"; the api package makes it succeed (enrichment-studio was ejected this way) | Fix that app: no detection, or `importlib.util.find_spec("application_sdk.main") is None` |
| A test asserted raw exception text in a response | Response bodies are redacted by design | Assert the typed error code / status instead |
| Host `uv lock` fails or moves a shared package | Every hosted app shares one environment (e.g. the api package needs `fastapi>=0.143.0,<0.144.0`; its `pandas<3` once moved the host from 3.0.5 to 2.3.3) | Align the app's ranges. A downgrade needs the affected app owner's confirmation; cap with a dated `constraint-dependencies` comment |
| `uv.lock` diff of thousands of lines, every source rewritten | A machine-wide uv config rewrote indexes to a registry proxy | Relock with `UV_NO_CONFIG=1 UV_DEFAULT_INDEX=https://pypi.org/simple` (`validation.md`) |
| `check` fails on `APPLICATION_NAME` | A read of the process-wide name; also matches comments | Use `self.context.app_name`; reword the comment |
| `fix` refuses and changes nothing | A shared line, a logger on an attribute, a mixed logger import, an `except` left as `pass`, or a logger used outside a log call | Edit those lines by hand (skill step 3), rerun |
| `fix` fails on a ruff finding | Its rewrite would leave an unused import or variable, or an undefined name; it restored every file | Fix the reported line by hand, rerun. Pre-existing findings are ignored |
| `fix` says it needs ruff | ruff not on PATH | `uv tool install ruff`, rerun. Nothing was changed |
| `build` / install cannot resolve `atlan-application-sdk-api` | Unreleased SDK and no git pin on the api package | Pin both packages to the same rev (skill step 4) |
| App release did not reach the host | Renovate has not opened the bump (7-day cooldown), the PR awaits review or relock, or the host does not pin the app yet | Find the Renovate PR on common-app-server; `no-serving-change` means the handler package is unchanged |
| After `pip install -U atlan-application-sdk` from a pre-split release, `application_sdk.handler` is missing | pip deletes files the old wheel owned that now belong to the api wheel | `pip install --force-reinstall --no-deps atlan-application-sdk-api`. uv is not affected |
