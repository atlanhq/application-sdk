# ADR-0009: Separate Handler and Worker Deployments

## Status
**Accepted**

## Context

Apps have two distinct modes of operation:
- **Handler**: HTTP-synchronous operations (auth, preflight, metadata) via FastAPI
- **Worker**: Durable workflow/activity execution via Temporal

We needed to decide how to deploy these components.

## Decision

We chose **separate Kubernetes deployments**: handlers and workers run as independent deployments with different scaling profiles.

**Environment variable convention**: All framework-specific variables use the `ATLAN_` prefix (e.g., `ATLAN_APP_MODULE`, `ATLAN_TEMPORAL_HOST`). Standard OpenTelemetry variables retain their `OTEL_` prefix.

## Options Considered

### Option 1: Separate Deployments (Chosen)

```
Handler Deployment (always-on)           Worker Deployment (scale 0→N)
┌──────────────────────────────┐         ┌──────────────────────────────┐
│ Deployment: my-app-handler   │         │ Deployment: my-app-worker    │
│ minReplicas: 1               │         │ minReplicas: 0 (KEDA)        │
│                              │         │ maxReplicas: 10 (KEDA)       │
│   --mode handler             │         │   --mode worker              │
│   FastAPI on :8080           │         │   Temporal worker            │
│   /auth, /preflight, etc.    │         │   Health on :8081            │
└──────────────────────────────┘         └──────────────────────────────┘
              │                                        │
              ▼                                        ▼
┌──────────────────────────────┐         ┌──────────────────────────────┐
│ Service: my-app-handler      │         │ KEDA ScaledObject            │
│ port: 80 → 8080              │         │ Scales on Temporal queue depth│
└──────────────────────────────┘         └──────────────────────────────┘
```

**Pros:**
- **Handlers always-on**: Min 1 replica ensures immediate HTTP responses (no cold start)
- **Workers scale to zero**: KEDA scales workers to 0 when task queues are empty
- **Independent scaling profiles**: Handlers scale on request rate, workers on queue depth
- **Cost efficiency**: Idle workers consume zero resources between runs
- **Fault isolation**: Handler issues don't affect worker processing and vice versa
- **Independent lifecycle**: Can restart/upgrade handlers without draining workflows

**Cons:**
- **More deployments**: Two deployments per app instead of one (mitigated by the Helm chart)

### Option 2: Sidecar Containers (Not Chosen)

Handler and worker run as containers in the same pod.

**Cons:**
- **No scale to zero**: Handlers must be always-on, so workers can't scale to zero
- **Coupled scaling**: Can't scale workers independently of handlers
- **Restart coupling**: Restarting handler also restarts worker (potential workflow disruption)

## Rationale

### Different scaling requirements

Handlers need immediate availability because users expect instant responses when testing credentials or fetching metadata. Cold start creates poor UX for interactive operations.

Workers can scale to zero because work is queued in Temporal and waits for workers. KEDA can spin up workers within seconds when work arrives. Most apps have bursty workloads with idle periods.

### Cost efficiency

For a typical deployment with 50 apps:
- Handlers: 50 pods (1 per app, minimal resources: ~128Mi/50m)
- Workers: 0–N pods per app based on actual load

With sidecar approach: 50+ pods minimum (can't scale to zero).

## Environment Variable Reference

### Framework variables (`ATLAN_` prefix)

| Variable | Description | Example |
|----------|-------------|---------|
| `ATLAN_APP_MODULE` | App class to load | `my_package.app:MyApp` |
| `ATLAN_TEMPORAL_HOST` | Temporal server address | `temporal:7233` |
| `ATLAN_TEMPORAL_NAMESPACE` | Temporal namespace | `default` |
| `ATLAN_TASK_QUEUE` | Task queue for workers | `my-app-queue` |
| `ATLAN_HANDLER_HOST` | Handler bind address | `0.0.0.0` |
| `ATLAN_HANDLER_PORT` | Handler HTTP port | `8080` |
| `ATLAN_HEALTH_PORT` | Worker health port | `8081` |
| `ATLAN_LOG_LEVEL` | Log verbosity | `INFO` |
| `ATLAN_SERVICE_NAME` | Service identifier | `my-app` |

### OpenTelemetry variables (`OTEL_` prefix — standard)

| Variable | Description |
|----------|-------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector endpoint (set to `$(K8S_NODE_IP):4317` by Helm) |
| `OTEL_SERVICE_NAME` | Service name for traces |
| `OTEL_RESOURCE_ATTRIBUTES` | Cluster/pod metadata attributes |

## Consequences

**Positive:**
- Handlers are always available for immediate HTTP responses
- Workers scale to zero, saving significant resources for idle apps
- Clear operational separation simplifies monitoring and debugging
- Consistent environment variable naming across all apps

**Negative:**
- Two deployments per app increases manifest complexity (mitigated by the `helm/atlan-app/` chart)

## Implementation

Both deployments use the same container image:

```bash
# Handler pod
python -m application_sdk.main --mode handler

# Worker pod
python -m application_sdk.main --mode worker
```

Health endpoints:
- **Handler** (`--mode handler`): FastAPI on `:8080` with `/health`, `/ready`
- **Worker** (`--mode worker`): lightweight HTTP server on `:8081` with `/health`, `/ready`

KEDA ScaledObject per worker, targeting its Temporal task queue:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
spec:
  minReplicaCount: 0
  maxReplicaCount: 10
  triggers:
    - type: temporal
      metadata:
        taskQueue: my-app-queue
        targetTasksPerWorker: "5"
```
