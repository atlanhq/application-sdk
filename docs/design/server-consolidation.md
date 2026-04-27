# Server Consolidation — Design Doc (DRAFT)

**Status:** in progress
**Last updated:** 2026-04-27
**Owner:** Sanil Khurana

## Context

Today each Atlan app runs its own always-on FastAPI server pod, per app per tenant. Validated via Grafana (`kube_pod_status_ready{namespace=~".+-app", pod=~".+-server-[a-f0-9]+-.+", condition="true"} == 1`):

- **6,767 Ready app-server pods** across 639 tenants × 78 apps
- **6,767 / 6,767** at exactly 1 replica per (tenant, app) — already minimum
- ~5,000 of the pods are 8 "core" apps that run on every tenant: `enrichment-studio`, `automation-engine`, `lineage`, `publish`, `atlan-auth`, `query-intelligence`, `popularity`, `native-migration`
- Memory request: 99.3% at 512 Mi; p99 actual usage 453 Mi (88% of request — tight)
- CPU request: 99% at 100m; p99 actual usage 39m (CPU is 3× over)
- Fleet aggregate: 3,578 GiB requested for 1,929 GiB actual; 714 cores requested for 55 cores actual

Server pods are now ~50% of total app cost (per `pod-app-runtime` thread `1773130815.749059`). They're the largest single line item because workers already scale to 0 via KEDA.

## Goals

- Reduce per-tenant server-pod footprint by collapsing the 8 always-on core apps into one process per tenant.
- Keep developer experience identical: dev still writes `class MyApp(App)` and runs `run_dev_combined(MyApp)` locally.
- No client-side code changes required (callers continue using existing K8s Service URLs).

## Non-goals (v1)

- Consolidating source-connector apps (dbt, bigquery, etc.) — they have install/uninstall lifecycles tied to tenant configuration. Defer to v2.
- Scale-to-zero for server pods — adds cold-start latency, separate trade-off discussion.
- Cross-tenant consolidation. Each tenant keeps its own runtime pod.
- Independent per-app rollouts. v1 restarts the whole tenant runtime on app updates; revisit if rollout cadence becomes painful.

## Core principles

1. App contains server logic. Developer can run app locally as a single process.
2. Zero developer friction — consolidation is invisible to the dev who writes the app.
3. Deployments are seamless — marketplace install/uninstall flow continues to work.

## High-level shape

A new repository — provisional name **`common-app-server`** — owns the consolidated runtime. It is a thin FastAPI host whose only job is to import per-app Routers (each app exposes its own router; the SDK provides the machinery to construct it) and dispatch incoming requests to the correct Router based on the Host header.

```
common-app-server (new repo)
├── src/main.py                 # FastAPI: /health, Host-header dispatch middleware
├── pyproject.toml              # depends on: atlan-redshift-app, atlan-publish-app, ...
└── At startup, imports come from each app's own package:
        from redshift_app.router import server_router
        from publish_app.router import server_router
        ...
    The SDK provides the router-construction primitive; each app's package
    exposes the built router as an importable attribute.
    The common-app-server then registers each Router under its Host pattern
    via Starlette Host routes.
```

K8s topology per tenant:
```
namespace: common-app-server  (single namespace, no per-app namespaces)
├── 1 Deployment: common-app-server   (the consolidated runtime pod)
└── N Services, one per supported app: bigquery, lineage, publish, ...
    All Services have selector → common-app-server pod
    Each Service exposes a stable DNS name; Host header on incoming
    requests is the discriminator the runtime dispatches on.
```

Per-app code: developers write their App class as today; the SDK already produces a per-app FastAPI sub-app via `create_app_handler_service()` (see `application_sdk/handler/service.py:347`). That helper will be refactored into a Router-returning factory so it can be mounted by the common-app-server.

**SDK mode changes:**
- `--mode worker` — kept (worker pods unchanged; KEDA-scaled-to-0 as today)
- Local dev mode — kept (the existing `run_dev_combined` path; spins up its own FastAPI for the single app the dev is working on; needs no Host dispatch)
- `--mode handler` (production server-only) — **removed**. Replaced by `common-app-server`. No production app deploys a per-app handler pod anymore.

## Open decisions

### D1 — Per-app Service topology — RESOLVED

**Decision: Single namespace.** All per-app Services live in the `common-app-server` namespace alongside the runtime Deployment. Per-app namespaces (`bigquery-app`, etc.) are deleted for the 8 core apps.

DNS shape: `bigquery.common-app-server.svc.cluster.local` (or whatever shared namespace is picked).

Day-1 client updates required:
- ~50 marketplace-packages YAML templates with hardcoded `http://{name}.{name}-app.svc.cluster.local:8000` URLs (search-and-replace).
- 2 Heracles hardcoded constants at `utils/constants.go:124-126`.
- Heracles per-tenant configmaps that bake the app-server URL.
- 1 callsite in `atlan-local-marketplace-app/src/catalog_service/core/catalog.py:593` that derives the app URL from the per-app namespace pattern.

Rationale: cleaner long-term topology; ExternalName CNAMEs added an extra DNS hop and per-app namespace clutter for backwards compatibility we don't actually need given the audit found a small finite set of callsites.

### D2 — How the runtime maps Host header → app router — RESOLVED

**Decision: explicit (App, k8s_name) tuples.** The runtime is told explicitly which apps it hosts and under what K8s service name:
```python
host_apps([
    ("redshift", redshift_router),
    ("publish",  publish_router),
    ("alpha",    alpha_router),
    ("beta",     beta_router),
])
```

Why explicit instead of deriving from `App.name` or `ATLAN_APPLICATION_NAME`:

1. **Drift audit found apps don't declare their own `App.name`.** They use the v2 SDK pattern `BaseSQLMetadataExtractionApplication(name=APPLICATION_NAME, ...)`, where `APPLICATION_NAME` is a module-level constant in `application_sdk/constants.py:36` populated from the `ATLAN_APPLICATION_NAME` env var at import time.
2. **`APPLICATION_NAME` is a process-level singleton.** With multiple apps in one process, this can't be each app's identity — it's the runtime's identity. The SDK refactor must pass app name to each app instance explicitly rather than relying on the env-var constant.
3. Explicit tuples also document which apps the runtime hosts and at what name — useful for ops, debugging, audit logs.

The K8s service convention (still derived from the second tuple element):
```
http://{k8s_name}.{shared_namespace}.svc.cluster.local
```

(post-D1 the namespace is shared, e.g. `common-app-server`.)

### D2 (legacy section, superseded above)

Whether the runtime maintains an explicit Host → app mapping, or derives it dynamically at startup from registered apps + a naming convention.

**Decision:** Dynamic at startup, **keyed on the K8s service name (= `ATLAN_APPLICATION_NAME` env / `.Values.name`)** — NOT on Python `App.name`. The two are independent today and can drift.

**Convention (corrected after tracing the production chart):**
```
http://{k8s_name}.{k8s_name}-app.svc.cluster.local:8000
```
Source of truth: `atlan/subcharts/atlan-app/templates/service.yaml:6-7` and `flux_utils.py:103-115`. `k8s_name` is what the marketplace deploys the app under; the chart sets it as `ATLAN_APPLICATION_NAME` env var (deployment.yaml:367-368).

Note: the Service name is just `{k8s_name}` (no `-server` suffix). The `-server` only appears in the Deployment and pod names.

**Two implementation options for `host_apps()`:**
- **Option 1** — explicit tuples: `host_apps([(BigQueryApp, k8s_name="bigquery"), ...])`. No drift possible.
- **Option 2** — SDK enforces `App.name == ATLAN_APPLICATION_NAME` at startup, fails fast on drift, then `host_apps([App1, App2, ...])` is unambiguous.

**Open:** which of the two. Pre-req: audit existing apps to see how often `App.name` already drifts from `.Values.name` in production.

**Rationale:** Apps shouldn't know about K8s topology, but they DO need a stable K8s identity. The convention has to key on the deployment-side identity (which is what callers actually use in URLs), not the Python class identity (which is a separate accidental duplicate).

### D4 — Deployment flow when an app is updated

Per-app code lives in its own repo (`atlan-publish-app`, `atlan-redshift-app`, etc.) with its own release cadence. With consolidation, the running process is `common-app-server`, not the app. So a per-app version bump no longer naturally triggers a redeploy.

**Option A (chosen for v1):** Manual pyproject.toml bump.
- App author cuts a release of their app (publishes a new wheel/version).
- They open a PR against `common-app-server` to bump the dependency in `pyproject.toml`.
- Merge to `common-app-server` triggers a rebuild + redeploy of the consolidated runtime.
- Pros: simple; one repo, one image, one release pipeline. Easy to reason about which app versions are running where.
- Cons: extra PR per app release; per-app release no longer ships independently.

**Option B (later, possibly v2):** Declarative version manifest + automation.
- A YAML/TOML file in `common-app-server` declares per-app pinned versions.
- A push to an app repo triggers automation that updates the manifest, rebuilds `common-app-server`, and redeploys.
- Pros: app authors don't write PRs against an unfamiliar repo; release cadence preserved.
- Cons: more pipeline machinery; cross-repo coupling needs careful design (auth, signing, rollback).

**Status:** Option A for v1. Revisit when per-app release friction becomes painful.

### D3 — Lifecycle of app install/uninstall

Whether the runtime can hot-add/remove apps, or whether install/uninstall triggers a runtime rolling restart.

**Decision (tentative):** v1 = rolling restart. Same blast radius as today's per-app deployment rollouts. Revisit if the cadence becomes painful.

## Resolved decisions

- **D1**: single namespace for all per-app Services + client updates (see D1 section above for callsite list).
- **D2**: explicit `(k8s_name, router)` tuples passed to `host_apps()`; do not rely on `App.name` or process-level `APPLICATION_NAME`.
- **D4**: app version updates via `pyproject.toml` bump in `common-app-server`; per-tenant pinning via custom branches.

## PoC validation (2026-04-27)

A standalone PoC at `/tmp/consolidation-poc/` proves the Host-dispatch design works as intended. 14 functional tests + a 500-concurrent-request stress test, all green.

### Confirmed

1. Starlette `Host()` route dispatches sub-apps by Host header. `from app_alpha.router import server_router` mounted under `Host("alpha.alpha-app.svc.cluster.local", app=server_router)` works.
2. Two sub-apps with the same path (`/workflows/v1/start`) coexist without collision; Host header is the discriminator.
3. Per-app `/health` reachable via the app's Host.
4. Pod-level `/health` (kubelet probes hitting pod IP) reachable via a parent-level route registered AFTER Host routes.
5. Unknown Host returns clean 404, not silent dispatch.
6. Port-suffixed Hosts (`:80`, `:8000`) match Starlette's Host pattern.
7. Case-insensitive Host matching works with a small ASGI middleware that lowercases the Host header before routing.
8. 500 concurrent requests interleaved across alpha/beta — zero crosstalk, zero race conditions.

### Findings that became design constraints

- **Route ordering is load-bearing.** Host routes MUST be registered in `parent.routes` BEFORE any parent-level path routes (e.g. `/health`). Otherwise the parent-level path matches first and Host dispatch never fires. Verified by initial test failure where `/health` with Host=alpha returned the parent's response instead of alpha's.
- **HTTP Host headers are case-insensitive (RFC 7230) but Starlette's `Host()` is string-equal.** Need an ASGI middleware that lowercases the Host header in the request scope before routing. Cheap; ~15 lines.
- **Trailing-dot FQDN does NOT match.** A request with `Host: alpha.alpha-app.svc.cluster.local.` (trailing dot — sometimes added by DNS clients) returns 404. Low risk in practice (K8s clients don't add the dot) but worth noting.

### PoC layout (artifact)

```
/tmp/consolidation-poc/
├── common_app_server/main.py      # ~70 lines: parent FastAPI, Host routes,
│                                    LowercaseHostMiddleware, /health
├── app_alpha/router.py            # FastAPI sub-app: /health, /workflows/v1/*
├── app_beta/router.py             # same shape
└── tests/
    ├── run_validation.py          # 14 functional checks
    └── run_concurrency.py         # 500 concurrent interleaved requests
```

This shape will be lifted into the real common-app-server repo when we cut PRs.

## Open questions

1. How does Heracles' live-log streaming behave when one pod hosts multiple apps? See audit caveat #1: `heracles/handler/runs.go:1422-1556` parses the K8s service URL into `(namespace, deployment)` to tail pod logs. Needs Heracles change to filter logs by app within a shared pod.
2. Dapr sidecar count — does one Dapr sidecar handle multiple app-ids (via Dapr's multi-app support), or do we run one per app?
3. Temporal worker — does one worker subscribe to all per-app task queues, or do we keep N small workers? Per-app keeps observability and KEDA scaling intact for workers.
4. Source-connector apps (dbt, bigquery, etc.) — when/how do we fold them in?

## Resolved (during discussion, captured here for trace)

- **Local dev** — keep the existing local-dev mode (`run_dev_combined`); it spins up a single-app FastAPI on the dev's machine. Production removes `--mode handler` entirely; local dev keeps it.
- **Per-tenant version skew** — handled by branching/forking the `common-app-server` image. A team that needs `bigquery@v1.5` for one tenant and `bigquery@v1.2` for another cuts a custom branch of `common-app-server` with the appropriate `pyproject.toml` pins, builds an image, and deploys it to those tenants. Same flexibility as today, shifted from per-app HelmRelease pinning to per-deployment common-app-server image pinning.
- **Memory accounting (e.g., loaded-but-unused connector imports)** — deferred. Not a v1 concern; revisit after PoC is running.

## Next steps

1. SDK: sketch `host_apps([App1, App2, ...])` entry point + Host-header dispatch middleware.
2. Helm chart: prototype `tenant-runtime` deployment + per-app ExternalName Services (if Option A) or single-namespace Services (if Option B).
3. PoC: pick 2 of the 8 core apps (suggest `enrichment-studio` + `popularity`), bundle into one runtime, deploy to one internal tenant (`markeznp25`), validate.
4. Generalize to all 8 core apps; ring-deploy to 5 tenants; fleet-wide rollout.

## Appendix — validated PromQL queries

Server pods per tenant per app:
```promql
count by (clusterName, namespace) (
  kube_pod_status_ready{namespace=~".+-app", pod=~".+-server-[a-f0-9]+-.+", condition="true"} == 1
)
```

Memory headroom (p99 usage / request):
```promql
quantile(0.99,
  container_memory_working_set_bytes{namespace=~".+-app", pod=~".+-server-[a-f0-9]+-.+", container!="POD", container!=""}
  / on(pod, namespace, clusterName) group_left()
  kube_pod_container_resource_requests{resource="memory", namespace=~".+-app", pod=~".+-server-[a-f0-9]+-.+"}
)
```

Datasource: `cd27b984-0b24-4adf-8bf8-1711c6b21061` on observability.atlan.com (VictoriaMetrics).
