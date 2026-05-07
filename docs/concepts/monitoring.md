# Monitoring

The Application SDK exposes Prometheus metrics covering workflow / activity
execution, HTTP server traffic, custom application metrics, and Temporal
SDK internals — all through a single endpoint per pod. Scrape configuration
differs by deployment topology (combined-mode pods are scraped directly;
worker-only pods push to a Pushgateway).

## Architecture

The SDK funnels every metric source into the in-process
`prometheus_client.REGISTRY` and exposes that registry to operators by
one of two transports, depending on pod role:

| Pod role | Transport | Endpoint |
|---|---|---|
| **Combined / handler** | Direct scrape | `http://<pod>:8000/metrics` (FastAPI) |
| **Worker** (split deployment) | Push | Pushgateway at `ATLAN_PROMETHEUS_PUSHGATEWAY_URL` |

The Temporal SDK's Rust-core Prometheus endpoint (`127.0.0.1:9464`) is
bound **loopback only** and is not externally reachable. The FastAPI
`/metrics` handler proxies it in-process for combined / handler pods;
the worker's `TemporalCoreCollector` reads it locally and includes the
families in each Pushgateway push. So scrape configurations always
target the FastAPI port (or the Pushgateway), never `:9464` directly.

### What's in the metric body

Every emitted series carries one inlined resource-attribute label —
`app_name` — so the most common operator query (filter by connector)
works without a `target_info` join. Per-release attributes
(`app_version`, `app_release_id`, `app_sdk_version`,
`app_release_channel`) and the rest of the OTel `Resource` travel via
the `target_info` gauge (one row per pod) and are recovered at query
time with `* on(instance) group_left(...) target_info`. See
[Metrics Standards](../standards/metrics.md) for the cardinality rules
that bound the rest of the label set.

The FastAPI `/metrics` endpoint (or a worker push) merges:

- **Custom metrics** from `record_metric()` calls and direct OTel meter
  use (`application_sdk.observability.metrics`)
- **HTTP server instrumentation** (FastAPIInstrumentor) using stable
  OTel HTTP semantic conventions: `http.request.method`, `http.route`,
  `server.address`, `network.protocol.version`,
  `http.response.status_code`. The metric is named
  `http_server_request_duration_seconds` (seconds, not milliseconds).
  Stable conventions are enabled via `OTEL_SEMCONV_STABILITY_OPT_IN=http`
  — set in the chart by default for v3+ apps.
- **Temporal SDK** families from `MetricsInterceptor` (counters and
  histograms emitted by activity/workflow execution) plus the Rust-core
  families proxied from `127.0.0.1:9464` (`temporal_request_*`,
  `temporal_workflow_task_*`, `temporal_sticky_cache_*`,
  `temporal_num_pollers`, etc.)
- **`prometheus_client` defaults** (`process_*`, `python_gc_*`,
  `python_info`)

### `ATLAN_ENABLE_TEMPORAL_CORE_METRICS`

This env var **does not gate the FastAPI `/metrics` route** — that
endpoint is always registered. The flag controls only whether the
Temporal Rust-core endpoint binds `127.0.0.1:9464`:

| Value | Effect |
|---|---|
| `true` (default) | Rust core binds 9464; FastAPI proxy + worker `TemporalCoreCollector` can read it |
| `false` | Rust core uses `Runtime.default()` (no Prometheus listener); FastAPI `/metrics` still serves SDK + HTTP + python defaults but lacks the `temporal_*` Rust-core families |

`run_dev_combined()` proactively sets it to `false` in local dev so a
hot-reload-restarted process doesn't fail to bind 9464 (which the
previous process is still holding in `TIME_WAIT`).

### Pushgateway for worker pods

Worker-only pods (split deployment) don't run a FastAPI server, so
Prometheus has no scrape target. Instead, they push the local registry
to a Pushgateway every `ATLAN_PROMETHEUS_PUSHGATEWAY_INTERVAL_SECONDS`
(default 30 s) plus a final push on shutdown. Configuration:

```bash
ATLAN_PROMETHEUS_PUSHGATEWAY_URL=http://prometheus-pushgateway.monitoring.svc.cluster.local:9091
ATLAN_PROMETHEUS_PUSHGATEWAY_INTERVAL_SECONDS=30
ATLAN_PROMETHEUS_PUSHGATEWAY_DELETE_ON_SHUTDOWN=true            # default
ATLAN_PROMETHEUS_PUSHGATEWAY_SWEEP_STALE_ON_START=true          # default
ATLAN_PROMETHEUS_PUSHGATEWAY_SWEEP_STALENESS_SECONDS=300        # default
ATLAN_PROMETHEUS_PUSHGATEWAY_HTTP_TIMEOUT_SECONDS=10            # default
ATLAN_PROMETHEUS_PUSHGATEWAY_SHUTDOWN_DELETE_DELAY_SECONDS=35   # default
```

Hardening behaviors that ship by default: graceful exits DELETE the
group from the gateway (no leak); each new worker sweeps stale
predecessor groups left by OOM/eviction; every gateway HTTP call is
bounded to 10 s; the final push is held for 35 s before DELETE so
Prometheus has a full scrape window. See [ADR-0012](../adr/0012-observability-consolidation.md)
for the design rationale.

### Available Temporal SDK metrics

A non-exhaustive subset of what flows through the Rust-core path:

| Metric | Description |
|--------|-------------|
| `temporal_activity_execution_latency` | Activity execution duration (histogram) |
| `temporal_activity_schedule_to_start_latency` | Time from activity schedule to start (histogram) |
| `temporal_workflow_completed_total` | Total completed workflows (counter) |
| `temporal_workflow_endtoend_latency` | End-to-end workflow duration (histogram) |
| `temporal_request_latency` | gRPC request latency to Temporal server (histogram) |
| `temporal_long_request_total` | gRPC long-poll requests (counter) |
| `temporal_worker_task_slots_available` | Available worker task slots (gauge) |
| `temporal_worker_task_slots_used` | In-use worker task slots (gauge) |
| `temporal_sticky_cache_hit_total` | Sticky cache hits (counter) |
| `temporal_sticky_cache_size` | Current sticky cache size (gauge) |

The full list is defined by the [Temporal Python SDK](https://docs.temporal.io/references/sdk-metrics).

### Prometheus scrape configuration (combined / handler pods)

In Kubernetes the `atlan-app` chart provisions a ServiceMonitor when
`metrics.enabled: true` is set in the app's values. The scrape uses the
Service's named port `http`:

```yaml
spec:
  endpoints:
    - port: http        # main Service declares 8000 as `name: http`
      path: /metrics
      interval: 60s     # configurable via metrics.interval
```

For raw Prometheus configurations, target the handler port directly:

```yaml
scrape_configs:
  - job_name: <app>-handler
    static_configs:
      - targets:
          - <handler-host>:8000
    metrics_path: /metrics
```

### Recommended Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| High activity failure rate | `rate(temporal_activity_execution_failed_total[5m]) > 0.05` | Warning |
| Worker slots exhausted | `temporal_worker_task_slots_available == 0` | Critical |
| Elevated workflow latency | `histogram_quantile(0.99, temporal_workflow_endtoend_latency_bucket) > 300` | Warning |
| Temporal server errors | `rate(temporal_long_request_failure_total[5m]) > 0` | Warning |

---

## Distributed Tracing (OTLP)

Enable OpenTelemetry trace export to send spans to your cluster's OTLP collector:

```bash
ENABLE_OTLP_LOGS=true                              # export log records via OTLP
OTEL_EXPORTER_OTLP_ENDPOINT=http://$(K8S_NODE_IP):4317  # node-local collector
ATLAN_ENABLE_OTLP_TRACES=true                      # export trace spans
# Metrics use the Prometheus path (FastAPI /metrics + Pushgateway).
# The previous OTLP metric exporter was removed in PR #1573.
```

In Kubernetes, set `OTEL_EXPORTER_OTLP_ENDPOINT` using the Downward API:

```yaml
env:
  - name: K8S_NODE_IP
    valueFrom:
      fieldRef:
        fieldPath: status.hostIP
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://$(K8S_NODE_IP):4317"
```

---

## Structured Logging and `self.logger`

`self.logger` is available in both `run()` and `@task` methods. It is automatically bound with:

| Field | Value |
|-------|-------|
| `app_name` | The app's kebab-case name |
| `workflow_run_id` | Temporal workflow run ID (canonical name; `run_id` is a backwards-compat alias) |
| `correlation_id` | Platform-level correlation identifier |

These fields appear on every log entry without any manual binding — use `self.logger` directly.

```python
class MyConnector(App):
    @task
    async def fetch_data(self, input: FetchInput) -> FetchOutput:
        self.logger.info("fetching page=%d", page_num)
        # Emits: {"level":"INFO","msg":"fetching page=3","app_name":"my-connector","workflow_run_id":"...","correlation_id":"..."}
```

Use **%-style** message bodies (`"fetching page=%d", page_num`) rather than keyword arguments. See [Logging Standards](../standards/logging.md) and [ADR-0011](../adr/0011-logging-level-guidelines.md).

---

## Correlation IDs

The `correlation_id` propagates automatically across:

- The handler (set by the incoming request from Heracles)
- All `@task` log entries (bound by the framework)
- Cross-app correlation (propagated via `correlation_id` when one App's output triggers another via Automation Engine DAG)

In Grafana/Loki, filter by `correlation_id` to see the full trace of a multi-phase connector run across all apps.

Access the current correlation ID programmatically:

```python
from application_sdk.observability import get_correlation_context

ctx = get_correlation_context()
cid = ctx.correlation_id if ctx else None  # str (empty when unset) or None when ctx is None
```

---

## Observability Store Sink

By default, logs, metrics, and traces are also written to parquet files in the object store under `artifacts/apps/{app_name}/{deployment_name}/observability/`. This enables historical querying even when the live pipelines (OTLP for logs/traces, Prometheus scrape / Pushgateway push for metrics) are unavailable.

Control with:

```bash
ATLAN_ENABLE_OBSERVABILITY_STORE_SINK=true   # default: true
```

Retention and batching:

```bash
ATLAN_LOG_RETENTION_DAYS=30
ATLAN_LOG_BATCH_SIZE=100
ATLAN_LOG_FLUSH_INTERVAL_SECONDS=10
```

See [Configuration](../configuration.md#logging) for all observability variables.
