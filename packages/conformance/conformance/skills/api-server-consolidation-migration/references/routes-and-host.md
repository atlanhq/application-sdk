# Routes, host behaviour and host onboarding

Read from the `api-server-consolidation-migration` skill, steps 7 and 8.
Sources: application-sdk `docs/standards/api-distribution.md`,
`application_sdk/handler/{routes,asgi,metadata_routes,object_routes}.py`, and
common-app-server `hosted-api-apps.toml`, `scripts/stage_api_apps.py`,
`common_api_server/main.py`.

## How a request reaches the app

The host routes on the **leading label of the Host header**
(`X-Original-Host` wins over `Host`), so callers do not change. Once an app
sets `routeToCommonAPIServer: true`, its Service is an `ExternalName` alias to
`common-api-server.common-api-server-app.svc.cluster.local` and **every**
Heracles call for that app lands on the host, not only handler calls. The host
must serve every route Heracles uses for the app, or that feature breaks.

## Routes the host serves

`build_asgi_app` (application-sdk `application_sdk/handler/asgi.py`) serves the
same routes the worker's handler service does, from one shared implementation
each.

| Heracles flow | App route | On the host |
|---|---|---|
| Liveness | `GET /health` | Served |
| Test credential | `POST /workflows/v1/auth` | The handler's `test_auth` |
| Preflight | `POST /workflows/v1/check` | `preflight_check` |
| Warmup tier | `POST /workflows/v1/warmup` | `warmup` |
| Metadata (filter dropdowns) | `POST /workflows/v1/metadata` | `fetch_metadata` |
| App-specific endpoints | whatever `Handler.routers()` returns | Served after the SDK's routes. A router that redefines an SDK path is refused at startup. |
| Setup form | `/workflows/v1/configmap/{id}`, `/configmaps` (Local Marketplace cache first, the app as fallback) | From `app/generated/` (`*.json`, `_input.py`) shipped in the staged package |
| Run submit | `POST /manifest`, `GET`/`POST /workflows/v1/manifest`. Automation Engine then runs the DAG on the worker through Temporal. | From `app/generated/`. The task queue is the one the worker polls: `ATLAN_APPENV__<APP>__ATLAN_TASK_QUEUE` if set, else derived from `ATLAN_APPENV__<APP>__ATLAN_DEPLOYMENT_NAME`, else from the host's own `ATLAN_DEPLOYMENT_NAME` (the chart gives the host and every worker the same tenant-wide `deploymentName`; Heracles re-stamps SDR runs' agent-side nodes itself). The host never uses its own bare `ATLAN_TASK_QUEUE` or `ATLAN_APPLICATION_NAME` for an app. Static manifests only. |
| Native-app API | `/workflows/v1/input-contract` | From the generated `_input.py` |
| Credential create/update, and every run submit before the manifest fetch | `GET`/`POST /workflows/v1/config/{guid}?type=credentials` | One S3 bucket from `ATLAN_S3_BUCKET` / `ATLAN_S3_REGION`, keyed `persistent-artifacts/apps/<app>/<type>/<id>/config.json` with the hosted app's name (the key its worker reads). **503 without `ATLAN_S3_BUCKET`.** |
| File fields | `POST /workflows/v1/file` | Same bucket. `prefix` must be relative segments with no `..` (400 otherwise); every upload lands under `persistent-artifacts/apps/<app>/uploads/`. |
| Workflow status for a non-Automation-Engine run | `GET /workflows/v1/status/{wf}/{run}` | **Not served** (FND-3164). Heracles calls it only when a status request is not `execution_mode=automation-engine`. `/workflows/v1/start` has no Heracles caller left, so the host does not serve it. |

**Do not route an app that needs an unserved row.** A canonical connector
needs none. Before the flag flips, the host pod needs an S3 role and
`ATLAN_S3_BUCKET` / `ATLAN_S3_REGION`; without them config answers 503 and every run
submit fails.

Behaviour matches the worker's routes before the split, except two deliberate
fixes: response-body text (error `detail`, envelope `message`,
`AuthOutput.message`, preflight messages, warmup `source_state`) goes through
`redact_secrets`, and `/auth`, `/check`, `/warmup`, `/metadata` answer a body
that is not a JSON object with
`422 {"success": false, "message": "Invalid request: body", "detail": []}`.

## Onboarding the app on the host (first time)

In common-app-server:

1. Add the app to `hosted-api-apps.toml`, `repo`, `tag`, `rev` on
   consecutive lines in that order (Renovate's regex manager matches them as
   one block). `tag` is the app's release tag, `rev` that tag's full
   40-character commit. Bump `[sdk]` only if the app needs a newer
   `gen_app_api.py`.
   ```toml
   [apps.<app>]                 # directory name under apps/, a DNS label
   repo = "atlanhq/<repo>"
   tag  = "<app release tag>"
   rev  = "<that tag's commit sha>"
   ```
2. In `pyproject.toml`, add `"atlan-<app>-api"` to `dependencies` and
   `atlan-<app>-api = { path = "apps/<app>" }` under `[tool.uv.sources]`.
   Remove the app's old `atlan-<app>-server @ git+…#subdirectory=server`
   dependency, if it has one.
3. `python scripts/stage_api_apps.py` (clones each pin, refuses a tag that no
   longer points at its `rev`, stages with the pinned SDK's `gen_app_api.py`
   into `apps/<name>/`), then relock (`validation.md`, "Relock").
4. A new transitive package younger than 7 days gets a dated
   `constraint-dependencies` cap with its reason. Never bypass the cooldown.

The Dockerfile stages in the same layer as `uv sync --locked`, then runs
`stage_api_apps.py probe --python .venv/bin/python`, which mounts every staged
app with the pinned SDK's `probe_app_api_member.py`. The staged package
registers the `atlan.app_server` entry point like any hosted app, so
discovery, per-app ejection, readiness and metrics are unchanged.

**Verify:** host unit tests pass (`tests/unit/test_stage_api_apps.py` checks
full SHAs and that every pinned app is a path dependency), `check-release-pins`
passes, and the `uv.lock` diff adds the app, drops its old `server` package and
keeps every registry source on `pypi.org`.

**Later releases.** Renovate (`renovate.json`, `github-tags`) opens the PR
moving `tag` and `rev` together, inheriting the org's 7-day release-age
cooldown. The `serving-change` workflow labels it `no-serving-change` when
every changed app's `gen_app_api.py hash` is unchanged and the SDK pin did not
move. Restage and relock on the PR before merging: Renovate does not relock.

## Host behaviour worth knowing

- **Degraded mount.** An app that fails to mount is ejected with a 503 naming
  the reason, and the rest keep serving. Readiness is "routing table built and
  at least one app mounted", so a broken app is a silent per-app outage unless
  someone alerts on `common_api_server_apps_mounted` against the expected count.
  The startup log says `Hosting N of M app server(s)`.
- **Fallback paths.** `/server/health`, `/server/ready`, `/health`, `/ready`
  and `/metrics` answer on the host's own name. An unknown Host gets
  `503 app_not_hosted`, never the pod's health.
- **Shared process.** Each app gets its own `run_in_thread` /
  `submit_in_thread` pool (8 threads by default), but the event loop, FastAPI's
  threadpool and the request concurrency limit are shared. Offload blocking
  calls with `run_in_thread`. Keep import-time and handler-construction work at
  zero: the mount phase is bounded by a timeout.
- **No Dapr, no secret store.** Handlers work from the credentials in the
  request; `self.context.get_secret()` raises `SecretStoreNotConfiguredError`.
- **Trust model.** Hosted apps share one process, one `os.environ` (every
  `ATLAN_APPENV__<APP>__*`), one AWS identity and one bucket, and the host
  trusts the Host header to pick the app. A hosted handler is trusted
  first-party code; the pod must be reachable only from in-cluster callers
  (NetworkPolicy).

## Rollback

| Problem | Action |
|---|---|
| One bad app | Set `routeToCommonAPIServer: false`. The app's own image serves the same handler through the SDK's service. Takes effect on the next reconcile, not instantly. |
| Bad app release | Move that app's `tag` and `rev` back to the previous release in `hosted-api-apps.toml` and roll the host. Other apps keep their pins. |
| Bad host | Revert the host image. |
