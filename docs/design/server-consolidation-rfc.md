# RFC: Server Consolidation — Cross-Repo Implementation Plan

**Status:** Draft
**Date:** 2026-04-27
**Owner:** Sanil Khurana
**Companion:** [`server-consolidation.md`](./server-consolidation.md) (architecture/decisions)

This RFC consolidates the server-consolidation design discussion into a single cross-repo implementation plan. It covers what changes in each repo, the migration path, the blind spots we've identified, and the open questions still to resolve.

## TL;DR

Per-tenant per-app FastAPI server pods (~6,767 fleet-wide, ~5,000 of them the 8 always-on core apps) consolidate into one `common-app-server` process per tenant. App devs write App classes as today; common-app-server imports each app's pre-built router; Host-header dispatch routes incoming requests to the correct app. Worker pods unchanged (still per-app, still KEDA-scaled). Estimated saving: ~75% reduction in server-side memory requests for the 8 core apps.

## Architecture summary

```
common-app-server (new repo)            HOSTED IN:
├── pyproject.toml (deps on app pkgs)   ┌─────────────────────────────────────┐
├── src/main.py                         │ namespace: common-app-server        │
│     - FastAPI parent                  │                                     │
│     - /health                         │ Deployment: common-app-server (1)   │
│     - Host-header dispatch via        │   ├── FastAPI host (Host-routing)   │
│       Starlette Host routes           │   ├── (no Dapr per-app — TBD)       │
│     - Imports:                        │   └── reads tenant app set at start │
│         from redshift_app.router      │                                     │
│             import server_router      │ Services (one per supported app):   │
│         from publish_app.router       │   ├── redshift   → port 8000        │
│             import server_router      │   ├── publish    → port 8000        │
│         ...                           │   ├── lineage    → port 8000        │
│                                       │   └── ... (one per app)             │
│                                       │ All Services select common-app-srv  │
└── Dockerfile                          └─────────────────────────────────────┘

Worker pods (per-app, per-namespace) — UNCHANGED. KEDA-scaled to 0 as today.
```

## What changes per repo

### `application-sdk` (this repo)

**Adds:**
- New module exposing a router-construction primitive. Each app calls it to build its server router from its `App` class.
- New SDK helper for the common-app-server side: `host_apps([(AppCls, k8s_name), ...])` → returns a Starlette parent app with Host dispatch.

**Removes:**
- `--mode handler` (production-only path) from main.py.

**Keeps:**
- `--mode worker` — workers unchanged
- Local-dev mode (`run_dev_combined`) — unchanged. Devs still spin up a single-app FastAPI on localhost.

**Helm chart (`helm/atlan-app/`):**
- Drops `handler-deployment.yaml` and `handler-service.yaml`.
- Keeps `worker-deployment.yaml`, `worker-scaledobject.yaml`, RBAC for workers.
- Atlanhq/atlan production chart (separate, more important — see below) gets analogous treatment.

### `common-app-server` (new repo)

Top-level, owned by the runtime team.

- `pyproject.toml` declares per-app dependencies on the supported core apps (`atlan-redshift-app`, `atlan-publish-app`, etc.) at pinned versions.
- `src/main.py` is small: builds Starlette parent, registers Host routes, exposes `/health`.
- Dockerfile produces the runtime image; pushed to `ghcr.io/atlanhq/common-app-server:<version>`.
- CI: dependency conflict check (`uv pip install` of all deps together), import test (verify all routers import without error), per-app smoke test.

### Per-app repos (`atlan-publish-app`, `atlan-redshift-app`, `atlan-mssql-app`, `atlan-lineage-app`, `atlan-automation-engine-app`, `atlan-query-intelligence-app`, etc.)

Each app adds a single new file:
```python
# {app_pkg}/router.py
from application_sdk.routing import build_server_router
from .app import MyApp   # the existing App subclass

server_router = build_server_router(MyApp)
```

Plus a `pyproject.toml` adjustment if the package isn't already structured to be a library (it should be).

**No App class changes.** Activities, handlers, workflows — all unchanged.

### `atlanhq/atlan` (production Helm)

`subcharts/atlan-app/templates/`:
- The production chart currently renders the per-app `-server` deployment when `splitDeploymentEnabled: true`. This template stops being used for the 8 core apps once they migrate.
- Two paths:
  - **D1 Option A (ExternalName)**: keep the `-server` Service template but switch it to `type: ExternalName` with a CNAME to common-app-server. Old per-app namespace stays.
  - **D1 Option B (single namespace)**: remove the per-app server Service template entirely; per-app Services live in `common-app-server` namespace.
- Worker template untouched.

`global-marketplace` / `marketplace-packages`:
- ~50 connector configmaps with hardcoded `http://{name}.{name}-app.svc.cluster.local:8000` URLs — these stay valid under D1 Option A; need search-and-replace under D1 Option B.

### `atlan-local-marketplace-app` (deployment orchestrator)

`src/deployment_orchestrator/`:
- `flux_utils.py` and `workflow.py` learn a new install/uninstall flow for "core apps" vs "connector apps":
  - Core apps: install means "register this app in tenant's common-app-server config + restart runtime"; no per-app HelmRelease.
  - Connector apps: unchanged for v1.
- `transform_config_to_helm_values()` in `config_parser.py` may need a new code path for core-app installs.
- The marketplace must mark apps as `core` vs `connector` somewhere (open question).

### `heracles` (separately owned — runtime team coordination required)

`handler/runs.go:1422-1556`:
- Live-log streaming parses the per-app service URL into `(namespace, deployment)` and tails K8s pod logs.
- For consolidated apps, the URL maps to `common-app-server` pod hosting 8 apps. Need to filter logs by app — likely a structured-log field added by the SDK and consumed by Heracles.

`utils/constants.go:124-126`:
- Hardcoded FQDNs for `KNOWLEDGE_APP_SERVICE_URL` and `AUTOMATION_ENGINE_UI_BASE_URL`. Stay valid under D1 Option A; need updating under D1 Option B.

## Migration plan

Staged rollout, not a flip:

1. **PoC (1 internal tenant, 2 apps).** Stand up common-app-server with `enrichment-studio` + `popularity` (low-risk apps). Deploy to `markeznp25`. Verify Host dispatch works, /health behaves, both apps respond on their old URLs.
2. **Validation (4–6 weeks).** Watch for issues. Compare metrics. Address blind spots that surface (secrets handling, Dapr context, signal handling).
3. **Expand to 8 core apps, internal tenants only.** Roll out across the dev tenant pool (`markeznp25`, `aplon95p04`, `enginemp07`, etc).
4. **Ring-deploy to production.** Per-tenant feature flag flips traffic from per-app handlers to common-app-server. Old handler pods stay running until cutover succeeds.
5. **Decommission per-app handler deployments.** Once a tenant is on common-app-server for ≥2 weeks with no regressions, remove per-app handler resources.

## Decisions

See [`server-consolidation.md`](./server-consolidation.md) for the running decision log. Open at time of writing:

| ID | Decision | Status |
|---|---|---|
| D1 | Per-app Service topology: ExternalName vs single namespace | open — both viable |
| D2 | Host→Router mapping mechanism (explicit tuples vs `App.name` enforcement) | open — needs drift audit |
| D3 | Install/uninstall lifecycle (rolling restart in v1) | tentative |
| D4 | Deployment flow when an app updates (pyproject.toml bump in v1) | chosen |
| D5 (new) | Per-app secrets handling — see Blind spot #1 | open |
| D6 (new) | Per-app Dapr context — see Blind spot #2 | open |
| D7 (new) | "Core" vs "connector" app classification source-of-truth | open |

## Blind spots / risks

Ranked by "likelihood to bite us" (full reasoning in main thread / CHANGELOG):

**High impact, address before locking design:**
1. Per-app K8s Secrets and ServiceAccount — biggest hidden lift; how do credentials reach a multi-app process?
2. Per-app Dapr context (state/secret/binding components scoped per app)
3. Per-app environment variables — apps using `os.environ` directly need a config-injection path
4. Signal handling (graceful shutdown across multiple apps in one process)
5. Per-app Prometheus metric labels — every metric needs an `app=` label, dashboards reference per-app series

**Important, address during PoC:**
6. Health check aggregation (one /health, eight apps' health states)
7. Failure isolation in shared async event loop
8. Worker model intentionally unchanged but tenant gets split topology (server ns ≠ worker ns)
9. Migration cutover plan — needs dual-mode operation in orchestrator
10. "Core vs connector" app classification — needs a source of truth

**Track but defer:**
11. Heracles log-streaming change (separate team coordination — Linear ticket needed)
12. Frontend asset routing under Host dispatch (probably works, verify)
13. Marketplace install flow rewrite for core apps (`flux_utils.py`)
14. Image build pipeline ownership and versioning for common-app-server
15. Resource sizing + VPA-recommend
16. Per-app rate limiting in the runtime
17. CI dependency-conflict detection
18. Frontend asset conflicts in shared image
19. Where does `atlan-local-marketplace-app` itself live (core or separate)?
20. Memory accounting (deferred per discussion)

## Per-PR scope summary (to-be-pushed)

Each row is a PR I'll prepare locally. None pushed until design locks and PoC runs.

| Repo | Scope | Size estimate | Blocks on |
|---|---|---|---|
| `application-sdk` | Add `application_sdk/routing.py` (router primitive + `host_apps` helper). Remove `--mode handler` from main.py. Drop handler-deployment/handler-service from `helm/atlan-app/`. | ~300 lines | D2 |
| `common-app-server` (NEW) | New repo. main.py, pyproject.toml, Dockerfile, basic CI. | ~150 lines + boilerplate | application-sdk PR landed first |
| `atlan-publish-app` | Add `app/router.py` exposing `server_router`. Bump app-sdk dep to consolidation branch. `uv sync`. | ~30 lines | application-sdk PR |
| `atlan-redshift-app` | Same as publish-app. | ~30 lines | application-sdk PR |
| `atlan-mssql-app` | Same. | ~30 lines | application-sdk PR |
| `atlanhq/atlan` (subcharts/atlan-app) | Drop `-server` deployment for core apps. Per-D1 choice: ExternalName Service OR remove. | ~50 lines | D1, PoC validated |
| `atlan-local-marketplace-app` | New install/uninstall path for core apps. `flux_utils.py` + `workflow.py` changes. | ~200 lines | D1, design locked |
| `heracles` (separate team) | Log-streaming changes to filter by app within a multi-app pod. | unknown | needs ticket + their owner |

## Suggested next steps

1. Run drift audit on the 8 core apps' `App.name` vs marketplace `app_name` to close D2.
2. Build a local PoC (just the SDK + common-app-server + 2 apps, on a dev machine) to validate Starlette Host routing actually works for our handlers.
3. Cut a Linear ticket; loop in Heracles team owner.
4. Run dependency-conflict CI dry-run (`uv pip install` of 8 core apps together) to catch any blockers.
5. Resolve D5 (secrets) and D6 (Dapr) before locking the SDK API.
6. Once PoC green: prepare PRs in branch-but-not-pushed state for review on a call.
7. Lock D1, push PRs in coordinated batch.

## Slack / standup talking points

- All 6,767 production server pods already at 1 replica; replica reduction not a lever.
- Memory is right-sized at 88% of 512 Mi p99; CPU is 3× over but it's a marginal win.
- Real lever: 8 core apps × 639 tenants ≈ 5,000 always-on pods → consolidate to 1 runtime pod per tenant. Memory savings ~75% on core apps.
- Architecture: thin FastAPI host (`common-app-server`) imports per-app routers, dispatches by Host header. Apps unchanged.
- Two open decisions, one pending audit, ~5 blind spots that need scoping in the SDK before we ship.
- PoC scope: 2 apps on 1 internal tenant, ~2 weeks of work before we know if the design holds.
