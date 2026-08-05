# Deployment

Every Application SDK app ships as a Docker image deployed to Kubernetes. The recommended shape is a **single pod** running `--mode combined` (worker + HTTP server in one process), KEDA-scaled on Temporal queue depth — see [ADR-0018](../adr/0018-single-pod-deployment-and-unified-check-core.md). The older split shape (a `handler` pod and a `worker` pod from the same image) still works and is what `splitDeploymentEnabled: true` produces.

This guide covers the `atlan.yaml` manifest, Dockerfile patterns, and the environment variables that drive production deployments.

---

## `atlan.yaml`

`atlan.yaml` at the repo root describes the app to the Atlan platform. The platform tooling (Global Marketplace, Local Marketplace, release pipeline) reads it to configure routing, Dapr, and deployment topology.

```yaml
# atlan.yaml — minimal example
app_id: my-connector
execution_mode: native
splitDeploymentEnabled: true
dapr:
  objectstore:
    enabled: true
  secretstore:
    enabled: true
```

### Top-level fields

| Field | Type | Description |
|-------|------|-------------|
| `app_id` | string | Unique identifier for this app. Must be kebab-case and match the Temporal task queue prefix. |
| `execution_mode` | enum | `native` — standard SDK handler + worker. `argo` — transitional value retained for legacy manifests; do not use for new apps. Will be removed in a future release. |
| `splitDeploymentEnabled` | bool | `true` → deploy handler and worker as separate Kubernetes Deployments (legacy; see [ADR-0018](../adr/0018-single-pod-deployment-and-unified-check-core.md)). `false` → one combined pod, now the recommended shape. |
| `self_deployed_runtime` | bool | `true` → publish the image to Docker Hub for self-hosted customers (SDR). Default: `false`. |

### `dapr` section

Controls which Dapr sidecar components are injected into the pods:

```yaml
dapr:
  objectstore:
    enabled: true          # object storage (required for file-based connectors)
  secretstore:
    enabled: true          # secret store (required if using credentials)
  statestore:
    enabled: false         # persistent state (optional)
  pubsub:
    enabled: false         # event-driven connectors (optional)
```

All components default to `false` unless listed. Only enable what your app actually uses — each enabled component adds a Dapr sidecar dependency and startup latency.

---

## Dockerfile

The SDK provides a base image with Python, uv, and Dapr pre-configured. Extend it:

```dockerfile
FROM registry.atlan.com/public/app-runtime-base:3

WORKDIR /app

# Copy dependency declarations first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Copy application code
COPY app/ app/

# Set the app module — startup fails without this
ENV ATLAN_APP_MODULE=app.connector:PostgresApp

# Generated contract artifacts path (matches app/generated/ in the repo)
ENV ATLAN_CONTRACT_GENERATED_DIR=app/generated
```

### Key Dockerfile conventions

- `ATLAN_APP_MODULE` **must** be set as an `ENV` in the Dockerfile. Startup hard-fails if it is absent.
- `ATLAN_CONTRACT_GENERATED_DIR` defaults to `app/generated` — override only if your directory layout differs.
- Keep app code in `app/` (importable as the `app` package). The handler auto-discovers `app.handler:MyHandler` if you name it correctly.

---

## Kubernetes Topology

### Recommended: one pod (`--mode combined`)

```
Deployment: my-app  (KEDA ScaledObject, scales 0 → N on Temporal queue depth)
└── command: application-sdk --mode combined --app app.connector:PostgresApp
    ├── Temporal worker — task queue atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME} (auto-derived)
    ├── Port 8000  — HTTP API (UI, Heracles, Automation Engine), Dapr pub/sub, /metrics, MCP
    └── Port 8081  — health endpoints; point k8s probes HERE, not at :8000
```

Point liveness/readiness at **`:8081`**, not `:8000`: the worker health server carries the
poll-loop liveness window (BLDX-1552) that can observe a silently stalled worker, which the
FastAPI probes cannot.

The pod scales to zero when the task queue is empty; each new workflow run — including an
interactive check, which arrives as a Temporal workflow — wakes it. Running the worker
alongside the HTTP server costs effectively nothing: the handler service already imports the
worker stack, so both together measure the same resident memory as the handler alone (see
[ADR-0018](../adr/0018-single-pod-deployment-and-unified-check-core.md) for the numbers). A
worker crash does not take HTTP down — combined mode supervises and restarts the worker
independently of uvicorn.

### Legacy: two pods (`splitDeploymentEnabled: true`)

```
Handler Deployment (min 1 replica, max N)
├── command: application-sdk --mode handler --app app.connector:PostgresApp
├── Port 8000  — HTTP API (UI, Heracles, Automation Engine)
└── GET :8000/metrics  — unified Prometheus endpoint (SDK + HTTP instrumentation + Temporal Rust-core)

Worker Deployment (KEDA ScaledObject, scales 0 → N)
├── command: application-sdk --mode worker --app app.connector:PostgresApp
├── Port 8081  — Worker health check endpoint
└── Connects to Temporal task queue: atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME} (auto-derived)
```

Both pods scale to zero in practice, which is why the split no longer buys what
[ADR-0009](../adr/0009-separate-handler-worker-deployments.md) intended. Worker-only mode has
no `/metrics` endpoint to scrape and pushes to a Pushgateway instead.

---

## Production Environment Variables

The full reference is in [Configuration](../configuration.md). The essential production set:

```bash
# Required
ATLAN_APP_MODULE=app.connector:PostgresApp
ATLAN_TEMPORAL_HOST=temporal.internal:7233
ATLAN_TEMPORAL_NAMESPACE=my-namespace

# Authentication (when Temporal requires auth)
ATLAN_AUTH_ENABLED=true
ATLAN_AUTH_TOKEN_URL=https://auth.internal/oauth2/token

# Observability
OTEL_EXPORTER_OTLP_ENDPOINT=http://$(K8S_NODE_IP):4317
ATLAN_ENABLE_TEMPORAL_CORE_METRICS=true                    # default; binds Temporal Rust core on loopback
# ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS=127.0.0.1:9464    # default; loopback-only, combined /metrics proxies it
# Worker-only pods (split deployment) push to a Pushgateway:
# ATLAN_PROMETHEUS_PUSHGATEWAY_URL=http://prometheus-pushgateway.monitoring.svc.cluster.local:9091

# Storage
ENABLE_ATLAN_UPLOAD=true
```

---

## Combined Mode (local / small apps)

The recommended production shape, and what local development uses:

```bash
application-sdk --mode combined --app app.connector:PostgresApp
```

`combined` mode runs the handler and worker in one process, sharing an event loop. It is what
SDR deployments have always run, and what [ADR-0018](../adr/0018-single-pod-deployment-and-unified-check-core.md)
adopts generally — a handler pod already carries the worker stack in memory, so the second pod
buys nothing. Locally, reach it through `run_dev_combined()`.

The one real caveat: HTTP request handling and the Temporal worker share an event loop, so a
high-volume app whose HTTP traffic is latency-sensitive can still prefer the split shape.
Note this is about *concurrency*, not memory, and that the check operations now arrive as
Temporal workflows rather than HTTP requests — which moves the interactive load off the HTTP
side. Blocking synchronous work in a handler or activity remains the thing that actually
hurts here (see [ADR-0010](../adr/0010-async-first-blocking-code.md)).

---

## Multi-App DAG (Automation Engine)

For connectors that run as an ordered sequence of apps (e.g. extract → publish), the platform's Automation Engine reads `app/generated/manifest.json` to build an execution DAG. See [Multi-App Coordination](multi-app-coordination.md) for details.
