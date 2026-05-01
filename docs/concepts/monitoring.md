# Monitoring

The Application SDK exposes built-in metrics for monitoring workflow execution health, worker capacity, and Temporal server connectivity via a Prometheus-compatible endpoint.

## Temporal Prometheus Metrics

Every application that uses `create_temporal_client()` automatically exposes ~40 built-in Temporal SDK metrics. No code changes are required.

### Endpoint

The metrics endpoint is bound at startup when `create_temporal_client()` is called:

```
http://<host>:9464/metrics
```

Default bind address: `0.0.0.0:9464` (OpenTelemetry Prometheus convention, port 9464).

Override with `ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS`:

```bash
ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS=0.0.0.0:9464
```

### Available Metrics

| Metric | Description |
|--------|-------------|
| `temporal_activity_execution_latency` | Activity execution duration (histogram) |
| `temporal_activity_schedule_to_start_latency` | Time from activity schedule to start (histogram) |
| `temporal_workflow_completed` | Total completed workflows (counter) |
| `temporal_workflow_endtoend_latency` | End-to-end workflow duration (histogram) |
| `temporal_request_latency` | gRPC request latency to Temporal server (histogram) |
| `temporal_request_failure` | gRPC request failures (counter) |
| `temporal_worker_task_slots_available` | Available worker task slots (gauge) |
| `temporal_worker_task_slots_used` | In-use worker task slots (gauge) |
| `temporal_sticky_cache_hit` | Sticky cache hits (counter) |
| `temporal_sticky_cache_size` | Current sticky cache size (gauge) |

The full list of metrics is defined by the [Temporal Python SDK](https://docs.temporal.io/references/sdk-metrics).

### Prometheus Scrape Configuration

Add the following job to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: temporal-sdk
    static_configs:
      - targets:
          - <app-host>:9464
```

For Kubernetes deployments using the Prometheus Operator, add a `ServiceMonitor`:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: temporal-sdk-metrics
spec:
  selector:
    matchLabels:
      app: <your-app-label>
  endpoints:
    - port: temporal-metrics
      path: /metrics
```

Expose the port in your `Service`:

```yaml
ports:
  - name: temporal-metrics
    port: 9464
    targetPort: 9464
```

### Singleton Runtime

The Temporal `Runtime` that binds the metrics port is a process-level singleton (`_prometheus_runtime` in `backend.py`). Multiple client instances or repeated `create_temporal_client()` calls within the same process reuse the same `Runtime` — the port is bound exactly once per process. See [Clients](clients.md#prometheus-metrics) for additional context.

## Application Metrics (Handler Service)

The handler service can expose an additional Prometheus endpoint at `/metrics` for application-level metrics (request counts, latency, error rates):

```bash
ATLAN_ENABLE_PROMETHEUS_METRICS=true  # default: true
```

When enabled, the handler service mounts a `/metrics` route served by `prometheus_client`. Scrape it alongside the Temporal metrics endpoint:

```yaml
scrape_configs:
  - job_name: app-handler
    static_configs:
      - targets:
          - <handler-host>:8000
    metrics_path: /metrics
```

### Recommended Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| High activity failure rate | `rate(temporal_activity_execution_failed[5m]) > 0.05` | Warning |
| Worker slots exhausted | `temporal_worker_task_slots_available == 0` | Critical |
| Elevated workflow latency | `histogram_quantile(0.99, temporal_workflow_endtoend_latency) > 300` | Warning |
| Temporal server errors | `rate(temporal_request_failure[5m]) > 0` | Warning |

---

## Distributed Tracing (OTLP)

Enable OpenTelemetry trace export to send spans to your cluster's OTLP collector:

```bash
ENABLE_OTLP_LOGS=true                              # export log records via OTLP
OTEL_EXPORTER_OTLP_ENDPOINT=http://$(K8S_NODE_IP):4317  # node-local collector
ATLAN_ENABLE_OTLP_TRACES=true                      # export trace spans
ATLAN_ENABLE_OTLP_METRICS=true                     # export metric points
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
| `run_id` | Temporal workflow run ID |
| `correlation_id` | Platform-level correlation identifier |

These fields appear on every log entry without any manual binding — use `self.logger` directly.

```python
class MyConnector(App):
    @task
    async def fetch_data(self, input: FetchInput) -> FetchOutput:
        self.logger.info("fetching page=%d", page_num)
        # Emits: {"level":"INFO","msg":"fetching page=3","app_name":"my-connector","run_id":"...","correlation_id":"..."}
```

Use **%-style** message bodies (`"fetching page=%d", page_num`) rather than keyword arguments. See [Logging Standards](../standards/logging.md) and [ADR-0011](../adr/0011-logging-level-guidelines.md).

---

## Correlation IDs

The `correlation_id` propagates automatically across:

- The handler (set by the incoming request from Heracles)
- All `@task` log entries (bound by the framework)
- Child-app calls (carried in the Temporal workflow headers)

In Grafana/Loki, filter by `correlation_id` to see the full trace of a multi-phase connector run across all apps.

Access the current correlation ID programmatically:

```python
from application_sdk.observability.correlation import get_correlation_context

ctx = get_correlation_context()
cid = ctx.correlation_id if ctx else None  # str (empty when unset) or None when ctx is None
```

---

## Workflow Log Export

For dual-export to both the platform OTLP collector and a tenant-level collector (S3 archival + live streaming):

```bash
OTEL_WORKFLOW_LOGS_ENDPOINT=http://tenant-collector:4317
ENABLE_OTLP_WORKFLOW_LOGS=true
```

When set, workflow log records are sent to both `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_WORKFLOW_LOGS_ENDPOINT`.

---

## Observability Store Sink

By default, logs, metrics, and traces are also written to parquet files in the object store under `artifacts/apps/{app_name}/{deployment_name}/observability/`. This enables historical querying even when the OTLP pipeline is unavailable.

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
