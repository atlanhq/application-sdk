---
name: api-server-consolidation-migration
description: Move a connector app's handler into an `api/` workspace member so the consolidated API host can serve it on atlan-application-sdk-api. Use when onboarding an app to the common API server.
---

# Serve an app's handler from the consolidated API host

The host installs `atlan-application-sdk-api` (a listed subset of
`application_sdk/`, see `docs/standards/api-distribution.md`) plus one small
package per app. The app's handler moves into that package. **Import paths do
not change**: `from application_sdk.handler import Handler` works on both
installs. There is one copy of the handler, and the worker's preflight gate and
the host run the same code.

## Steps

1. **Create the member.** `git mv` the handler, the failure classes it raises,
   and everything it imports from the app (client, SQL files, constants) into
   `api/<app>_api/`, for example `api/atlan_mysql_api/`. The import name must be
   unique per app, because the host loads every app into one process. Never use
   `app.*` there.
2. **`api/pyproject.toml`:**
   - `dependencies = ["atlan-application-sdk-api[<extras>]", <drivers>]`, where
     the extras are `sql`, `pandas` and `aws` as the handler needs them. Never
     depend on `atlan-application-sdk`.
   - `[project.entry-points."atlan.app_api"]` with `<service-name> = "<app>_api:handler"`,
     where `handler` is a module-level instance.
   - Force-include the configmap JSON from `app/generated/` if the host serves it.
3. **Root `pyproject.toml`:** add the member to `[tool.uv.workspace]`, depend on
   it, and point `[tool.uv.sources]` at it (`workspace = true`).
4. **Worker imports:** `app/` imports the handler and client from `<app>_api`.
   Nothing under `api/` imports `app`.
5. **The one import that changes:** `run_in_thread` from
   `application_sdk.execution.heartbeat` loads the Temporal layer. Inside `api/`,
   import it from `application_sdk.common.concurrency` instead. It is the same
   function.
6. **Check it the way CI will:**

   ```bash
   uv venv /tmp/api-only && uv pip install --python /tmp/api-only ./api
   /tmp/api-only/bin/python <sdk>/.github/scripts/probe_app_api_member.py \
       --name <service-name> --package <app>_api
   ```

   A `ModuleNotFoundError` names a worker-only import in handler code. Replace
   it with its api-listed equivalent, or ask for the SDK file to be listed
   (`check_api_surface.py` shows what that takes).

## Before an SDK release that includes the api distribution

Pin both packages to the same SDK git commit:

```toml
atlan-application-sdk = { git = "https://github.com/atlanhq/application-sdk.git", rev = "<sha>" }
atlan-application-sdk-api = { git = "https://github.com/atlanhq/application-sdk.git", rev = "<sha>", subdirectory = "packages/api" }
```

After the release, drop both sources and pin the released version.
