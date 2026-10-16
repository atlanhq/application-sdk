---
name: api-server-consolidation-migration
description: >
  Move an Atlan app onto the shared common-app-server host (API Server
  Consolidation) by serving the app's REAL handler from app/ through
  atlan-application-sdk-api. The app commits one [tool.atlan-app-api] block in
  pyproject.toml and no packaging; application-sdk's gen_app_api.py fixes,
  checks and stages the handler, and the host pins the app's release in
  hosted-api-apps.toml. Clears conformance rule P061 (HostedHandlerMigration):
  an app not yet migrated, a leftover hand-written server/ package on
  atlan-server-sdk, a [tool.atlan-app-api] handler that is not used the way the
  host needs, and a request-time compute_manifest hook. Covers which routes the
  host serves, prerequisites, the step-by-step checklist with a verification
  per step, host onboarding, local and internal-tenant validation, the
  routeToCommonAPIServer flip (image before flag), deleting server/ (only after
  the host's pin moved), rollback and troubleshooting. Use when onboarding an
  app to the consolidated host, replacing an app's server/ package, when P061
  fires, or when a hosted app 503s, returns app_not_hosted, is ejected, or
  fails the api-member CI job.
mandatory_triggers:
  - "/api-server-consolidation-migration"
  - "P061"
  - "HostedHandlerMigration"
  - "move this app to the consolidated API host"
  - "migrate to common-app-server"
optional_triggers:
  - "delete the server/ package"
  - "atlan-app-api block"
  - "routeToCommonAPIServer"
  - "app_not_hosted"
  - "api-member job failing"
owner: connector-platform-team
last_updated: "2026-10-16"
staleness_days: 60
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
  - sdk_checkout: "an application-sdk checkout at the commit or release the app pins (for .github/scripts/gen_app_api.py)"
outputs:
  - pyproject.toml ([tool.atlan-app-api] block; SDK pins)
  - uv.lock (relocked against pypi.org with the repo's uv version)
  - app/ handler code (relative imports, moved imports, typed errors instead of handler logging)
  - a common-app-server PR pinning the app's release in hosted-api-apps.toml
  - server/ deleted, once the host serves the new package
---

## Conformance rules this skill clears

P061. This rule names this skill as its `remediation_reference`, and
`/remediate` hands its findings here. When the skill is done, run
`atlan-application-sdk-conformance detect --rule P061` and confirm it is no
longer reported. P060 (logging in the handler module) is cleared on the way, by
step 2, but it has its own guide.

Which part of the checklist each P061 finding needs:

| P061 finding | Steps |
|---|---|
| `<Handler> is not served from the consolidated API host yet` | 0 to 9, then 10 if the app has a `server/` package |
| `server/ is a hand-written handler copy on atlan-server-sdk` | 0 to 9 if the app is not migrated yet, then **10 only after step 6 has merged and the host's pin serves the new package**. Deleting `server/` earlier takes the app's API offline on every tenant that still routes to the old package. |
| `[tool.atlan-app-api].handler … does not name a class …`, `data pattern … matches nothing` | 1 |
| `Absolute import …`, `… is absent on the host`, `APPLICATION_NAME is the host's own name …` | 2 (`fix`), then 4 (`check`) |
| `compute_manifest runs app code per request …` | Not hostable yet. Turn the hook's output into static data in the generated manifest, or leave the app on its own server until the host supports hooks. Do not suppress it. |

# Serve an app's handler from the consolidated API host

`common-app-server` serves many apps' HTTP surfaces from one process per
tenant, so an app stops running its own always-on API-server pod. Requests are
routed by the **leading label of the Host header** (`X-Original-Host` wins over
`Host`), so callers do not change.

## Why this approach (and what it replaces)

The earlier consolidation made each app keep a **second, hand-written copy** of
its handler in a `server/` folder built on `atlanhq/server-sdk`. That copy
drifted from the real handler. The new approach serves the app's **real
handler** from `app/`. There is no duplicate code and no committed packaging.

| Piece | What it is | Where |
|---|---|---|
| `atlan-application-sdk-api` | A listed subset of `application_sdk/` (handler, contracts, errors, credential specs, the shared routes, `run_in_thread`, SQL client base, AWS helpers), published as its own wheel. The two wheels are **disjoint**: `atlan-application-sdk` excludes these files and depends on `atlan-application-sdk-api` at the same version. It uses the **same import paths** (`from application_sdk.handler import Handler`). It has no Temporal, Dapr, object store, loguru or OpenTelemetry. Extras `sql`, `pandas`, `aws` and `pyatlan` use the SDK's own pins. | application-sdk `packages/api/`, `docs/standards/api-distribution.md` |
| `[tool.atlan-app-api]` | One config block in the app's root `pyproject.toml`. It is the only thing the app adds. | app repo |
| `gen_app_api.py` | `fix` / `check` / `build` / `stage` / `hash` (Python 3.11+). It turns the config into the package `atlan_<app>_api`. | application-sdk `.github/scripts/` |
| `hosted-api-apps.toml` | The host's pin of each app's **release tag and that tag's commit**, plus the application-sdk release that stages them. Renovate bumps them. | common-app-server |
| `api-member` CI job | Proves on every app PR that the handler mounts and serves the way the host serves it. | application-sdk `tests-reusable.yaml` |

> **Status (2026-10).** All reference PRs are open drafts, not merged:
> application-sdk #4058 (the slim package, every host route below, the
> scripts and the release step), atlan-mysql-app #778 (the reference
> migration), atlan-metabase-app #721 and atlan-redshift-app #486 (the same
> migration), and common-app-server #121 (host staging and release pins).
> `atlan-application-sdk-api` is **not on PyPI yet**. Until #4058 is released,
> pin it by git (step 3).

## Which routes the host serves for an app

Once an app sets `routeToCommonAPIServer: true`, its Service becomes an
`ExternalName` alias to the host. **Every** Heracles call for that app then
lands on the host, not only the handler calls. So the host must serve every
route Heracles uses, or that feature breaks for the app.

| Heracles flow | App route | On the host |
|---|---|---|
| Liveness | `GET /health` | ✅ `build_asgi_app` |
| Test credential | `POST /workflows/v1/auth` | ✅ in #4058 |
| Preflight | `POST /workflows/v1/check` | ✅ in #4058 |
| Warmup tier | `POST /workflows/v1/warmup` | ✅ in #4058 |
| Metadata (filter dropdowns) | `POST /workflows/v1/metadata` | ✅ in #4058 |
| App-specific endpoints | whatever `Handler.routers()` returns | ✅ in #4058, served after the SDK routes. A router that redefines an SDK path is refused at startup. |
| Setup form | `/workflows/v1/configmap/{id}`, `/configmaps`. The Local Marketplace cache is used first, with the app as fallback. | ✅ in #4058, from `app/generated/` shipped in the wheel |
| Run submit | `POST /manifest`, `GET`/`POST /workflows/v1/manifest`. After this, Automation Engine runs the DAG on the worker through Temporal. | ✅ in #4058, from `app/generated/` shipped in the wheel. The task queue is the one the app's worker polls: `ATLAN_APPENV__<APP>__ATLAN_TASK_QUEUE` if set (the worker's own override), else derived from `ATLAN_APPENV__<APP>__ATLAN_DEPLOYMENT_NAME`. The host never reads its own bare `ATLAN_*` values for an app; with neither per-app key it stamps no queue and logs an ERROR, so the host's app-env sync must carry them. Static manifests only: `check` refuses a `compute_manifest` hook. |
| Native-app API | `/input-contract` | ✅ in #4058, from the generated `_input.py` |
| Credential create/update, and every run submit before the manifest fetch | `POST /workflows/v1/config/{guid}?type=credentials` | ✅ in #4058, on S3 from `S3_BUCKET` / `S3_REGION`, keyed by the app's name at the path the worker reads. 503 without `S3_BUCKET` |
| File fields | `/file` | ✅ in #4058, on the same S3 bucket. The `prefix` is validated (no `..`, no absolute path, 400 otherwise), and on the host every upload lands under `persistent-artifacts/apps/<app>/uploads/`. |
| Workflow status for a non-Automation-Engine run | `GET /workflows/v1/status/{wf}/{run}` | ⏳ FND-3164. Heracles calls it only when a status request is not `execution_mode=automation-engine`. `/workflows/v1/start` has no Heracles caller left (native creates and delete-user go through Automation Engine), so the host does not serve it. |
| Publish the handler wheel on app release | (release flow) | ✅ in #4058 + common-app-server #121. The SDK pushes nothing. The host **pulls**: it pins each app's release tag + SHA, Renovate opens the bump PR, and the host's `serving-change` workflow labels it `no-serving-change` when `gen_app_api.py hash` shows the staged handler package is unchanged. |
| A tests-reusable leg that tests through the host route | (CI) | ✅ in #4058: the `api-member` job probes every route above through the app's own wheel |

**Do not route an app that needs a ⏳ row until that row ships.** A canonical
connector needs none of them. Before flipping the flag, the host pod also needs
an S3 role and `S3_BUCKET` / `S3_REGION`; without them config answers 503 and
every run submit fails.

Runs are started by Automation Engine, not by the app's `/start`. The **worker
is unchanged** and still runs the handler in-process for the run-start
preflight gate and for Self-Deployed Runtime (the `sdr:*` workflows). **Never
delete the handler** because "the host serves it now".

## Who can migrate now

| Fits now | Does not fit yet: first bring it to the canonical shape (the SDK adds no special cases) |
|---|---|
| Connector apps shaped like the reference apps (openapi, mysql, metabase): a `Handler` subclass in `app/`, a contract-generated `app/generated/`, and **no** custom middleware, MCP mounts, lifespan or startup hooks, or `compute_manifest` that must run at request time | Custom middleware or MCP mounts, startup or lifespan hooks, request-time `compute_manifest`, heavy routes outside the handler |

Non-connector apps (enrichment studio, governance studio, ai-memory) are
tracked separately under milestone **M4**.

**What a migrated app gets:** no per-tenant API-server pod (saving memory and
cost), no `server/` copy to keep in sync, one handler for both the worker and
the host, and CI that proves the host path on every PR.

---

## Migration checklist

Work in order. Each step ends in a check that must pass before the next.
Placeholders: `<sdk>` is an application-sdk checkout at the commit you pin,
`<app>` is the routing name and `<project>` is the root `[project].name`.

- [ ] **0. Prerequisites**
- [ ] **1. Add the config block**
- [ ] **2. `fix`, then review the removals**
- [ ] **3. Pin both SDK packages to one commit** (before the SDK release)
- [ ] **4. `check`, `build`, install alone, probe**
- [ ] **5. After the SDK release: re-pin to the release**
- [ ] **6. Host PR: add the app to `hosted-api-apps.toml`**
- [ ] **7. Validate locally against the real host**
- [ ] **8. Validate on an internal tenant**
- [ ] **9. Flip `routeToCommonAPIServer` (image before flag)**
- [ ] **10. Delete the old `server/` folder**

### 0. Prerequisites

- The app is in the canonical shape (see "Who can migrate now"), and the
  handler is a class in `app/`.
- The SDK includes #4058, either the release that ships it or a git pin (step 3).
- No handler file reads `APPLICATION_NAME` or `ATLAN_APPLICATION_NAME`. On the
  host those name the **host**, so use `self.context.app_name` instead:

  ```bash
  grep -rnE '\b(ATLAN_)?APPLICATION_NAME\b' app/
  ```

**Verify:** the grep finds nothing in the handler's files (`check` in step 4
enforces this for exactly the files it ships).

### 1. Add the config block

In the root `pyproject.toml`:

```toml
[tool.atlan-app-api]
handler = "app.handler:MySQLAppHandler"     # must be app.<module>:<Class>
data = ["app/sql/test_authentication.sql"]  # non-Python files the handler reads (globs ok)
dependencies = ["aiomysql>=0.3.0"]          # the handler's own third-party deps
extras = ["sql", "pandas", "aws"]           # atlan-application-sdk-api extras it needs
# name = "ai-memory"   # only when the Service name is not <project> minus atlan- / -app
```

How names are derived from `[project].name` (not the repo name). For
`atlan-mysql-app`:

| | Value | Source |
|---|---|---|
| Routing key, entry-point name, `app.state.app_name` | `mysql` | `name =`, or the project name minus `atlan-` and `-app` |
| Distribution / package | `atlan-mysql-api` / `atlan_mysql_api` | the project name with `-app` replaced by `-api`. `name =` does **not** change it. |

**Verify:** the routing key equals the app's k8s Service name and the leading
Host label. Every `data` glob must match something. `check` fails on an empty
match.

### 2. `fix`, then review

```bash
python3 <sdk>/.github/scripts/gen_app_api.py fix     # from the app repo root; needs ruff on PATH
```

What it rewrites, only in the handler's import closure inside `app/`:

| Change | Scope |
|---|---|
| `from app.x import y` → relative (`from .x import y`). The same file is `app.handler` on the worker and `atlan_<app>_api.handler` on the host. | all closure files |
| `application_sdk.execution.heartbeat.run_in_thread` → `application_sdk.common.concurrency` (same function, no Temporal) | all closure files |
| `application_sdk.observability.logger_adaptor.get_logger` → `application_sdk.common.logger` (structured on the worker, stdlib on the host) | all closure files |
| **Deletes logging** (logger binds, `logger.*` calls, `self.context.log_*`, and an import only when every name it imports is a logger). An emptied block gets `pass`. It prints every deletion. | **handler module only** (conformance **P060**, tier `block`) |

Other `app/` files the handler imports (a shared client, for example) also run
on the worker, so **they keep their logs**. Only their `get_logger` import
moves.

**Review every removal.** A hosted handler reports through its return value
(`AuthOutput`, `PreflightOutput` and its checks, `MetadataOutput`) or a typed
`AppError`, and the SDK's shared routes log every outcome with the request id.
Where a log line was the **only** report of a failure, raise a typed `AppError`
or return a failed check instead. Also remove comments and variables that only
existed for the deleted lines.

`fix` **refuses** rather than guesses, and then changes nothing. Edit by hand
and rerun when it reports any of:

- a log call that shares its line with other code;
- a logger kept on an attribute (`self.logger = ...`);
- a logger import that also imports other names (`get_logger`, or
  `getLogger` / `logger` from `logging` / `loguru`);
- an `except` block that would be left as only `pass` (raise a typed `AppError`
  from it instead);
- a removed logger that is still used somewhere other than a log call.

It needs ruff on PATH and fails without it. After rewriting, it runs ruff; if
its edits **introduced** an F401, F841 or F821 finding it restores every file and
fails. It rewrites only real import statements, never text inside a
docstring. `check` also runs P060 on the handler module.

**Verify:** the app's unit tests pass unchanged (mysql #778: 173 passed, no
test changes), and conformance P060 reports 0 findings.

### 3. Pin both SDK packages to one commit (before the SDK release)

`build` writes the wheel's dependency from `[tool.uv.sources]`. With a git pin
it copies the pin. Without one it asks PyPI for `atlan-application-sdk-api`,
which does not exist before the release. So pin **both** packages to the
**same** commit first, or step 4 cannot install:

```toml
[tool.uv.sources]
atlan-application-sdk     = { git = "https://github.com/atlanhq/application-sdk.git", rev = "<sha>" }
atlan-application-sdk-api = { git = "https://github.com/atlanhq/application-sdk.git", rev = "<sha>", subdirectory = "packages/api" }
```

Then run `uv lock`. Read [uv lock rewrites](#troubleshooting) before you commit
the lock.

**Verify:** the `uv.lock` diff changes only the two SDK entries.

### 4. `check`, `build`, install alone, probe

```bash
python3 <sdk>/.github/scripts/gen_app_api.py check
# → "gen_app_api: N app/ files ship as atlan_<app>_api"   (use this package name below)
python3 <sdk>/.github/scripts/gen_app_api.py build --out /tmp/dist-api
uv venv /tmp/api-only && uv pip install --python /tmp/api-only /tmp/dist-api/*.whl httpx2   # test-client dep, as the api-member job installs
/tmp/api-only/bin/python <sdk>/.github/scripts/probe_app_api_member.py \
    --name <app> --package atlan_<app>_api
```

`check` fails on any of these:

- an absolute `from app` import among the shipped files
- a worker-only import that `fix` would move
- any line matching `APPLICATION_NAME` or `ATLAN_APPLICATION_NAME`

`build` stages the closure plus `data` as `atlan_<app>_api/`. It generates an
`__init__.py` that exposes `handler` and `get_asgi_app()`, and a
`pyproject.toml` that registers the entry points `atlan.app_api` and
`atlan.app_server`. Then it builds the wheel. (`stage --out DIR` does the same
without building, and is what the host uses.)

The probe mounts the wheel exactly as the host does. It fails on any of these:

- the full `atlan-application-sdk` is installed
- the handler is not a `Handler`
- `app.state.app_name` is not `<app>`
- `/health` is not 200
- a malformed `/workflows/v1/auth` body does not return 422
- an empty-credentials auth call returns a bare 500
- `/check` or `/metadata` with empty credentials hit an `ImportError`, meaning
  a worker-only import that only breaks on the host

**Verify:** the probe prints `passes the host matrix`. In CI, `check` runs in
the `detect-api-member` job and build plus probe run in `api-member`. Both jobs
exist only once #4058 is on application-sdk `main`, because tests-reusable runs
from `@main`. Until then, this local run is the only proof.

### 5. After the SDK release

Remove both git sources, and raise the `atlan-application-sdk` floor in
`[project].dependencies` to the release. The handler wheel's
`atlan-application-sdk-api` range is copied from that specifier.

**Verify:** run `uv lock` and step 4 again. They must pass with no git sources.

### 6. Host PR (common-app-server)

1. Add the app to `hosted-api-apps.toml` with **full 40-character SHAs**. Bump
   `[sdk].rev` if your app needs a newer `gen_app_api.py`.
   ```toml
   [apps.<app>]                 # directory name under apps/, a DNS label
   repo = "atlanhq/<repo>"
   tag  = "<app release tag>"
   rev  = "<that tag's commit sha>"
   ```
2. In `pyproject.toml`, add `"atlan-<app>-api"` to `dependencies`, and add
   `atlan-<app>-api = { path = "apps/<app>" }` under `[tool.uv.sources]`.
   **Remove** the app's old `atlan-<app>-server @ git+…#subdirectory=server`
   line, if it has one.
3. Stage, then lock:
   ```bash
   python scripts/stage_api_apps.py          # clones each pin, runs gen_app_api.py stage → apps/<name>/
   UV_NO_CONFIG=1 uv lock --default-index https://pypi.org/simple
   ```
4. Add `constraint-dependencies` caps for any new transitive package younger
   than 7 days. Record the date and reason in a comment so the cap can be
   lifted. Never bypass the cooldown.

Pin a **release**: `tag` is the app's release tag and `rev` that tag's full
commit, in this order on consecutive lines (Renovate's regex manager matches
them as one block). The stage script refuses a tag that no longer points at its
rev. To validate unreleased code, put the commit in
`hosted-api-apps.overrides.toml` (`rev` only) and build with
`--build-arg ATLAN_API_APPS_ALLOW_OVERRIDES=1`. The `release-pins` check fails
while that file exists, so it never merges.

The Dockerfile runs `stage_api_apps.py` in the same layer as
`uv sync --locked`, with the short-lived git token. The staged package
registers the same `atlan.app_server` entry point as any hosted app, so
discovery, per-app ejection, readiness and metrics are unchanged. Each app gets
its own `run_in_thread` pool of 8 threads on the host, so one app's stuck calls
cannot stall the others (`run_in_thread` and `submit_in_thread` both use it).
FastAPI's own threadpool is still shared. The image build then runs
`stage_api_apps.py probe`, which imports and serves every staged app.

**Verify:** the host unit tests pass, including `test_stage_api_apps.py`, which
checks that every pin is a full SHA and that every pinned app is a path
dependency. The `uv.lock` diff adds your app, removes the old `server/` package
and keeps every source on `pypi.org`.

> After onboarding, the host pulls new releases. Renovate opens a PR moving
> `tag` and `rev` together. The `serving-change` workflow labels it
> `no-serving-change` when the staged handler package's hash is unchanged and
> the SDK pin did not move. Restage and `uv lock` on the PR before merging:
> Renovate cannot re-lock yet. Release pins inherit the 7-day
> `minimumReleaseAge`.

### 7. Validate locally against the real host

Run the real host with the full locked environment (every hosted app), not just
yours. This catches resolution conflicts and cross-app interference.

```bash
uv sync --locked && uv run uvicorn common_api_server.main:app --port 8000
H='Host: <app>.<app>-app.svc.cluster.local:8000'
curl -s localhost:8000/server/ready                                   # <app> listed, failed list empty
curl -s -H "$H" -XPOST localhost:8000/workflows/v1/auth  -d @good-creds.json
curl -s -H "$H" -XPOST localhost:8000/workflows/v1/check -d @check.json
curl -s -H "$H" -XPOST localhost:8000/workflows/v1/metadata -d @creds.json
curl -s -H "$H" localhost:8000/openapi.json | jq -r .info.title       # must be <app>: cross-talk check
curl -s -H 'Host: nope.nope-app.svc.cluster.local' localhost:8000/x   # 503 app_not_hosted
```

Point the app at a real or containerized source. Use synthetic credentials,
and never commit or paste them.

**Verify:** the log shows `Hosting N of N app server(s)`. Anything less means
an app was ejected, and the log names it and the reason. Good credentials
return 200. Bad credentials return a typed 4xx that **does not echo the
password**. `/check` returns 200 with a verdict, never a 500.

### 8. Validate on an internal tenant

Do **not** patch the live `common-api-server` Deployment, because it serves
other apps on that tenant. Use a throwaway pod instead:

- **Namespace:** the app's own (`<app>-app`), where its secret references
  resolve.
- **Image:** a host image built from your host branch (build-only, not
  published to a release channel).
- **Env:** copy the env **spec** from the host Deployment (references, never
  values), so the pod reproduces the host condition, with
  `ATLAN_APPLICATION_NAME` set to the host's name.
- **Command:** the host's uvicorn entry point.

**Verify:** run the step-7 probes by Host header, and diff status **and body
shape** against the app's own live server. A 200 with empty data hides a
broken route. Then check that `kubectl logs <pod> | grep -c ModuleNotFoundError`
is 0. Delete the pod afterwards and confirm the tenant is as you found it.

### 9. Flip the flag (image before flag)

Do not start this step until every route the app needs is served (see the
route table).

1. Merge the host PR and let the host image roll to the tenant. **This step
   ships on its own:** a pin only mounts the app, and no traffic moves yet.
2. On the tenant's real host pod:
   - `/server/ready` lists the app with an **empty `failed`** list.
   - `openapi.json` `info.title` per Host label equals that app.
   - `/workflows/v1/check` against an unreachable source returns **200 with a
     not-ready verdict**, not a 500.
   - an unknown Host still returns `503 app_not_hosted`.
3. Only then, in the app's `atlan.yaml`:
   ```yaml
   deploy:
     routeToCommonAPIServer: true
   ```
   This aliases the app's Service to
   `common-api-server.common-api-server-app.svc.cluster.local` and **removes
   the app's server Deployment**. A targeted app release to one tenant then
   changes only the worker. Serving code reaches the tenant only through the
   host's pin.

**Rollback:**

| Problem | Action |
|---|---|
| One bad app | Set `routeToCommonAPIServer: false`. The app's own image serves the same handler through the SDK service. It takes effect on the next reconcile, so it is not instant. |
| Bad app code | Move that app's `tag` and `rev` back to the previous release in `hosted-api-apps.toml` and roll the host. Other apps keep their pins. |
| Bad host | Revert the host image. |

### 10. Delete the old `server/` folder

Do this only after the host serves the new package (step 6 is merged and the
`server/` line is gone from the host). Delete the app's `server/` package and
any CI, Dockerfile extras or `[tool.uv.sources]` entries that existed only for
it. **Keep `app/handler.py`**, because the worker's preflight gate and the SDR
workflows still call it.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `503` with `reason: import_error` | Importing the staged package failed: usually a worker-only import at module level, or a dependency missing from `dependencies`/`extras` | Run step 4 locally. Use the api-shipped path, or ask for the SDK module to be added to `[tool.atlan-api.seeds]` |
| `503 factory_error` | `get_asgi_app()` raised, for example the handler `__init__` touched config or network | Read the host log. Keep handler construction side-effect free |
| `503 name_mismatch` / `missing_app_name` | The entry-point name ≠ `app.state.app_name`. This happens with a hand-edited staged package or a wrong `name =` | Fix `name =`. Never edit the generated `__init__.py` |
| `503 invalid_app`, `lifespan_error`, `lifespan_timeout` | The factory returned a non-ASGI object, or a lifespan raised or hung | The canonical shape has no lifespan. Remove it (see "Who can migrate now") |
| `503 app_not_hosted` | The flag was flipped before the host image mounted the app, or the Host label ≠ entry-point name | Step 9 ordering, step 1 naming |
| `/check` or `/metadata` 500 with `ModuleNotFoundError` | A deferred worker-only import on a request path (Temporal, Dapr, `application_sdk.storage`…) | The probe in step 4 catches this. Move the import to the api path |
| `SecretStoreNotConfiguredError` / `ObjectStoreNotConfiguredError` | The host has no secret store and no object store | Use request credentials. For config and file, set `S3_BUCKET` / `S3_REGION` and give the host pod an S3 role |
| Another app is ejected once yours is on the host | **Mixed host:** that app treats `import application_sdk` succeeding as "I am on the worker". The api wheel now makes that import work, so it loads worker-only modules | Fix the other app's detection. common-app-server #121 is blocked on exactly this for enrichment-studio |
| Host `uv lock` fails or moves a shared package | Every hosted app shares **one** environment. Examples: the SDK (3.46.0 and later) needs `fastapi>=0.143.0,<0.144.0`, and the SDK's `pandas<3` moved the host from 3.0.5 to 2.3.3 | Align ranges across apps. Get the affected app owner to confirm a downgrade. Cap with a dated `constraint-dependencies` comment |
| `uv.lock` diff of thousands of lines with every source rewritten | A managed `~/.config/uv/uv.toml` rewrote indexes to a credentialed proxy that CI cannot reach | `UV_NO_CONFIG=1 uv lock --default-index https://pypi.org/simple` |
| `check` fails on `APPLICATION_NAME` | A read of the process-wide name. It also matches comments | Use `self.context.app_name`, and reword the comment |
| `fix` refuses and changes nothing | A case it will not guess at: a shared line, a logger on an attribute, a mixed import, an `except` left as `pass`, or a logger used outside a log call | Edit those lines by hand (see step 2), then rerun |
| `fix` fails on a ruff finding | Its rewrite would have left an unused import or variable, or an undefined name; it restored every file | Fix the reported line by hand, then rerun. Pre-existing findings are ignored |
| `fix` says it needs ruff | ruff is not on PATH | `uv tool install ruff`, then rerun. Nothing was changed |
| App release did not change the host | Renovate has not opened the bump yet (7-day `minimumReleaseAge`), the bump PR is waiting for review or relock, or the host does not pin the app yet | Find the Renovate PR on common-app-server. `no-serving-change` on it means the handler package is unchanged |
| `pip install -U atlan-application-sdk` from a pre-split release leaves `application_sdk.handler` missing | pip removes files the old wheel owned that now belong to the api wheel | `pip install --force-reinstall --no-deps atlan-application-sdk-api`. uv is not affected |

## Host behaviour worth knowing

- **Degraded mount.** An app that fails to mount is ejected with a 503 naming
  the reason, and the rest keep serving. Readiness is "routing table built and
  at least one app mounted", so a broken app is a **silent per-app outage**
  unless someone alerts on `apps_mounted` versus the expected count.
- **Fallback paths.** `/server/health`, `/server/ready`, `/health`, `/ready` and
  `/metrics` answer on the host's own name. An unknown Host gets
  `503 app_not_hosted`, never the pod's health.
- **Shared process.** Blocking IO or unbounded memory in one handler hurts
  every hosted app. Offload blocking calls with `run_in_thread`, which uses the
  app's own pool. Keep import-time work at zero, because the mount phase is
  bounded by a timeout.
- **No Dapr and no secret store** on the host. Handlers work from the
  credentials in the request.
- **Trust model.** Hosted apps share one process, one AWS identity and one
  bucket, and the host trusts the Host header to pick the app. There is no
  secret isolation between hosted apps: a hosted handler is trusted code. The
  pod must be reachable only from in-cluster callers (NetworkPolicy). See
  "Trust model on the host" in `docs/standards/api-distribution.md`.
