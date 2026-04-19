# ADR-0003: Per-App Observability with Correlation-Based Tracing

## Status
**Accepted**

## Context

Each app runs in its own worker. Observability (logs, traces, metrics) is configured at startup to export to an OTLP endpoint — in the current SDK, this is the cluster's central OTLP collector, reached via the node's IP (`$(K8S_NODE_IP):4317`), wired automatically by the Helm chart.

When multiple apps execute as part of the same pipeline (e.g., orchestrated by Automation Engine), a question arises: how do we link each app's trace to the shared execution so the full run can be reconstructed?

## Decision

We chose **per-app observability with correlation-based tracing**: each app exports to its own OTLP service name, and distributed traces are linked via a propagated `correlation_id` and OpenTelemetry trace context.

## How It Works

Each app exports to the central OTLP collector under its own `OTEL_SERVICE_NAME`:

```
RelationalAssetsPipeline Worker       Loader Worker
├── OTEL_SERVICE_NAME: relational-assets-pipeline-worker
├── Logs: "Starting pipeline"         Logs: "Loading 500 records"
├── correlation_id: abc-123           correlation_id: abc-123 (propagated via AE)
│
└── Logs: "Pipeline complete"         └── Logs: "Load complete"
```

To reconstruct the full execution, query your observability backend by `correlation_id=abc-123` — logs and spans from both apps are returned.

## Options Considered

### Option 1: Per-App Service Names with Correlation (Chosen)

Each app configures its own `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` at startup. Trace context (`correlation_id`, `trace_id`, `span_id`) propagates across app boundaries automatically.

**Pros:**
- **App-centric observability**: Each app owner sees all activity for their app in one service
- **App-scoped insights**: Each app owner sees consolidated usage for their app — useful for capacity planning and debugging
- **Standard pattern**: Aligns with distributed tracing in microservices
- **Simple implementation**: No dynamic exporter configuration needed
- **Performance**: Long-lived exporters with batching; no per-request overhead

**Cons:**
- **Cross-app debugging requires correlation queries**: Must query by `correlation_id` to see full execution
- **Tooling dependency**: Requires an observability backend that supports cross-service trace correlation

### Option 2: Dynamic OTLP Routing Per-Request (Not Chosen)

Pass the parent's OTLP endpoint in workflow context; child apps route logs to the parent's service.

**Cons:**
- Fights OpenTelemetry design (exporters are long-lived, not per-request)
- Performance overhead from creating exporters per-request
- Loses app owner visibility into their own app's usage patterns

## Rationale

1. **Service ownership**: Each app owner is responsible for their app's behavior. Having all of an app's logs in one service name — regardless of who called it — supports this model.
2. **Shared app visibility**: For shared apps like Loader, seeing consolidated usage from all callers is valuable: which callers generate the most load, common failure patterns, optimization opportunities.
3. **Correlation is standard**: Every major observability platform supports querying by trace ID or correlation ID. This is the expected pattern for distributed systems.
4. **Simplicity**: The framework propagates correlation context automatically through the `_correlation_id` field on input Pydantic models.

## Consequences

**Positive:**
- App owners have complete visibility into their app's behavior
- Standard distributed tracing patterns apply
- No per-request overhead for observability routing

**Negative:**
- Debugging cross-app issues requires correlation-based queries

## Implementation

- `self.logger` in both `run()` and `@task` automatically includes `app_name`, `run_id`, and `correlation_id` on every entry
- `correlation_id` propagates from parent to child via the `_correlation_id` field on input Pydantic models
- OpenTelemetry trace context propagates automatically through Temporal interceptors
- The Helm chart sets `OTEL_RESOURCE_ATTRIBUTES` with `k8s.cluster.name`, `k8s.pod.name`, `k8s.node.name`, `k8s.namespace.name`, `k8s.workflow.name`, and `k8s.workflow.package.version` on each pod
- Workers set `OTEL_EXPORTER_OTLP_ENDPOINT` to `$(K8S_NODE_IP):4317` so telemetry flows to the cluster's node-level OTLP collector

**To query a full distributed trace:**

```
# In your observability backend (Grafana, Datadog, etc.)
correlation_id = "abc-123"
# Returns logs/spans from all apps that participated in this execution
```
