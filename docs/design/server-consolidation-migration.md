# Server Consolidation — Migration Plan & Cutover

**Status:** Draft for review
**Date:** 2026-04-27
**Owner:** Sanil Khurana
**Linear:** [ARUN-550](https://linear.app/atlan-epd/issue/ARUN-550) (under [ARUN-342](https://linear.app/atlan-epd/issue/ARUN-342))

This doc plans the staged rollout of the server-consolidation work
([ARUN-342](https://linear.app/atlan-epd/issue/ARUN-342)) across
internal and customer tenants. Companion docs:
[server-consolidation.md](./server-consolidation.md) (architecture +
decisions) and
[server-consolidation-rfc.md](./server-consolidation-rfc.md) (cross-repo
implementation plan).

## Why staged

The change has high blast radius: every tenant's HTTP-handling topology
moves from N per-app pods to one shared `common-app-server` pod. A
silent regression hits every core-app surface for that tenant. We
mitigate with a per-tenant feature flag, a ring-deploy schedule, and a
rollback story that reverts a single HelmRelease values diff.

The flag is the `coreApp.enabled` boolean in the atlan-app subchart
(landed in ARUN-547). The orchestrator passes it through from the
marketplace app config (ARUN-549). Flipping a tenant's flag from
`false` → `true` is the cutover; flipping back is the rollback.

## Pre-cutover gates

These must all be green before stage 2:

- [ ] **SDK PR merged** — ARUN-543 (`application_sdk.routing.host_apps`
      + tests + handler-mode removal). Must be on a tagged release.
- [ ] **common-app-server image** built and pushed to GHCR — ARUN-545.
      All 8 core apps' routers importable in one Python process; CI
      dep-conflict check green.
- [ ] **Per-app router PRs merged** — ARUN-546 expanded from the
      initial 3 (publish, redshift, mysql) to all 8 core apps:
      enrichment-studio, automation-engine, lineage, publish, atlan-auth,
      query-intelligence, popularity, native-migration. Each app's
      release is tagged and pinned in `common-app-server/pyproject.toml`.
- [ ] **Helm chart change live** — ARUN-547 merged to atlanhq/atlan,
      cut into a release that all target tenants are running.
- [ ] **Orchestrator change live** — ARUN-549 merged to
      atlan-local-marketplace-app, deployed.
- [ ] **Marketplace generator updated** — ARUN-548. New core-app
      registrations emit the right URL by default.
- [ ] **Heracles change scoped** — ARUN-551. Doesn't strictly block
      stage 2 (logs still tail; they just intermix), but the team
      should be aware before stage 4 (customer beta).
- [ ] **Smoke runbook** — written, including kubectl commands to
      verify per-app dispatch on a cutover tenant (see Appendix).
- [ ] **Rollback runbook** — verified on the dev tenant in stage 2.
- [ ] **Observability** — Grafana dashboard panel showing per-tenant
      `common-app-server` pod memory + CPU + p99 request latency,
      separate panel for the residual per-app `-server` pods (they
      should disappear post-cutover).

## Stages

### Stage 0 — Image rollout (no functional change)

Deploy the `common-app-server` image to all target tenants alongside
the existing per-app server pods. **No traffic goes to it yet** —
`coreApp.enabled` stays `false` everywhere. The image just becomes
warm and pullable; the `common-app-server` namespace exists but holds
nothing.

**Done when:** the runtime image pulls cleanly and the `common-app-server`
deployment is in `Available=True` state on every target tenant.

**Risk:** very low. No traffic shift.

### Stage 1 — Single internal-dev cutover

Pick **`markeznp25`** (median dev tenant from the App Cost analysis;
already used as the canary in past KEDA / split-deployment rollouts).

For each of the 8 core apps installed on `markeznp25`, flip the app's
HelmRelease values to `coreApp: { enabled: true }`. The atlan-app
subchart then:
- skips the `<app>-server` Deployment
- renders `Service: <app>` in the `common-app-server` namespace (selector
  pointing at the runtime pod)

The runtime pod was already there from Stage 0; now traffic actually
flows to it.

**Verify:**
- `kubectl get pods -A -l app.kubernetes.io/component=server` should
  show **no** pods in any `<app>-app` namespace on this tenant.
- `kubectl get pods -n common-app-server` shows 1 Ready pod.
- `kubectl get svc -A | grep -E '(publish|lineage|...)'` shows
  Services in `common-app-server` namespace.
- A round of Atlan UI smoke tests for each core app: auth,
  preflight, metadata fetch, workflow start, workflow result.
- Heracles live-log streaming: hit each app, confirm logs surface
  (with the known caveat that they intermix until ARUN-551 lands).
- `kube_pod_status_ready{namespace="common-app-server"} == 1` on
  Grafana.

**Watch for** (Day 0 → Day 7):
- p99 request latency on `common-app-server` vs the historical p99
  on per-app `-server` pods. Tolerance: ≤2× original (the per-app
  pods used 100m CPU but were 99% idle; the runtime is shared so
  some queueing is fine, but big regressions are not).
- Memory: actual usage vs the runtime's `requests` value. Should
  trend at ~1.5–2 GiB based on p50 of 281 Mi × 8 ≈ 2.2 GiB cap.
- OOM kills (`kube_pod_container_status_last_terminated_reason="OOMKilled"`)
  on the runtime pod. Zero is the goal; one means we underprovisioned.
- Failed activities, increased Sentry/Mezmo error rates on the apps.

**Done when:** 7 days of green metrics, no user-reported regressions.

**Rollback:** flip `coreApp.enabled` back to `false` per app. Flux
re-renders the per-app `-server` Deployment; pod comes up; old
Service moves back to per-app namespace; traffic resumes its old path.
~5 minutes per app.

### Stage 2 — Internal dev pool

Roll out to the rest of the App-BU shared internal tenants from
[bu-apps Slack][bu-apps] (revampdbt, app-partners,
apps-framework-redshift-testing, partner-staging, pipelines10,
partner-apps, warelines-models, apps-enterprise, warehouse-bq-routines).

[bu-apps]: https://atlanhq.slack.com/archives/C088M0KD1LH/p1764771505661059

These run real engineering workloads but no customer data. ~2 weeks of
soak time with the same metrics watch as Stage 1.

**Done when:** 14 days green across all 9 dev tenants.

### Stage 3 — Beta ring (customer tenants, low-volume)

Pick ~10 customer tenants from beta channel that:
- Are on Standard infra tier (not Enterprise — let them ride the
  later stages)
- Have <1 GiB/day workflow throughput (low downside if anything regresses)
- Have a customer-success owner who can be looped in on regressions

Per-tenant cutover follows Stage 1's verify/watch/rollback cycle.

**Done when:** 14 days green across the beta ring.

### Stage 4 — Production fleet, batched

Roll out to the remaining ~620 customer tenants in batches of 50–100
per day. Sequence by:
- Standard infra tier first; Enterprise last
- Tenants with fewest core apps installed first
- Tenants with the largest Atlan footprint last

Per-batch verify: dashboards, smoke checks. Pause if any single tenant
regresses; investigate before continuing.

**Done when:** 100% of fleet on coreApp=true for all 8 core apps.

### Stage 5 — Decommission

For tenants on consolidation for ≥14 days with no regressions, prune
the residual per-app artifacts:
- Old per-app `-server` Deployments (already not rendered, but any
  orphaned ReplicaSets/Pods can be cleaned up).
- Per-app namespaces' inbound NetworkPolicies that allowed traffic
  into the now-gone server pods.
- Heracles per-tenant configmaps that pointed at the old URLs (no-op
  if the URL was kept identical-via-alias, breaking if not).

Update the marketplace generator's defaults so new core-app registrations
emit `coreApp: { enabled: true }` from day one.

**Done when:** zero `<app>-server-...` pods exist anywhere in the fleet
for the 8 core apps.

## Rollback procedures

### Per-app, per-tenant (5 minutes)

The flip is in the marketplace app config (which the orchestrator
translates to HelmRelease values):
```yaml
# marketplace app config — `coreApp: { enabled: false }` is the rollback
coreApp:
  enabled: false
```

Re-trigger the deployment: orchestrator generates a new HelmRelease
without `coreApp.enabled`. Flux reconciles; per-app `-server` pod
comes back up; Service moves back to per-app namespace; clients
resolve to the original FQDN.

No workflow-state loss because workflow state lives in Temporal, not
the runtime pod.

### Whole-tenant (10 minutes)

Same as per-app, applied to all 8 core apps for the tenant via a
single config push. Flux reconciles all 8 HelmReleases in parallel.

### Fleet-wide (1 hour)

Revert the atlan-app subchart change in atlanhq/atlan (ARUN-547),
cut a release, deploy. `coreApp.enabled` is no longer honored;
chart falls back to today's per-app `-server` rendering. Each tenant
gets the next reconcile cycle.

The runtime pods stay running but now have no traffic; can be deleted
manually or left until clean-up.

## Owners + comms

- **Driver:** Sanil Khurana (App Runtime team)
- **Reviewers / signoff for each stage:** Anbarasan, Tianchu, Anuj
- **Heracles changes:** [ARUN-551](https://linear.app/atlan-epd/issue/ARUN-551)
  owner (TBD — needs Slack outreach to find current Heracles owner)
- **Comms cadence:** weekly Slack update in `#pod-app-runtime` for the
  team; cross-post to `#bu-apps` at stages 1, 3, 4 start, and 5 done.

## Appendix — verify commands

Per-tenant smoke after a cutover (`<TENANT>` = e.g. `markeznp25`):

```bash
# 1) No per-app -server pods left for core apps
for app in publish redshift mysql lineage automation-engine \
           query-intelligence popularity atlan-auth enrichment-studio \
           native-migration; do
  pods=$(kubectl --context <TENANT> get pods -n "${app}-app" \
         -l app.kubernetes.io/component=server --no-headers 2>/dev/null | wc -l)
  if [ "$pods" -gt 0 ]; then
    echo "FAIL: $app still has $pods server pods"
  fi
done

# 2) common-app-server has 1 Ready pod
kubectl --context <TENANT> get pods -n common-app-server \
  -l app.kubernetes.io/name=common-app-server

# 3) Each core app's Service exists in common-app-server namespace
kubectl --context <TENANT> get svc -n common-app-server

# 4) Hit each app's /health via the cluster-internal URL
for app in publish redshift mysql; do
  kubectl --context <TENANT> exec -n common-app-server deploy/common-app-server -- \
    curl -sS -H "Host: ${app}.common-app-server.svc.cluster.local" \
         http://127.0.0.1:8000/health
  echo
done
```

## Appendix — fleet metrics (for the Grafana dashboard)

Pre-cutover baseline (per-app server pods):
```promql
count(kube_pod_status_ready{namespace=~".+-app", pod=~".+-server-[a-f0-9]+-.+", condition="true"} == 1)
```

Post-cutover (consolidated runtime):
```promql
count(kube_pod_status_ready{namespace="common-app-server", condition="true"} == 1)
```

Per-tenant memory savings (delta = old req - new actual):
```promql
sum by (clusterName) (
  kube_pod_container_resource_requests{namespace=~".+-app", container=~".+-server", resource="memory"}
)
-
sum by (clusterName) (
  container_memory_working_set_bytes{namespace="common-app-server", container!="POD"}
)
```

Per-tenant `common-app-server` p99 request latency (wire up to
runtime's Prometheus scrape — may need an extra metric label rollup if
multiple apps share the same HTTP path).
