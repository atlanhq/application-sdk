# Monitoring

The Application SDK exposes built-in metrics for monitoring workflow execution health, worker capacity, and Temporal server connectivity via a Prometheus-compatible endpoint.

## Temporal Prometheus Metrics

Every application that uses the SDK worker automatically exposes ~40 built-in Temporal SDK metrics. No code changes are required.

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

The Temporal `Runtime` that binds the metrics port is a process-level singleton (the SDK runtime, initialized once per process by `create_worker()`). The port is bound exactly once per process.

### Recommended Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| High activity failure rate | `rate(temporal_activity_execution_failed[5m]) > 0.05` | Warning |
| Worker slots exhausted | `temporal_worker_task_slots_available == 0` | Critical |
| Elevated workflow latency | `histogram_quantile(0.99, temporal_workflow_endtoend_latency) > 300` | Warning |
| Temporal server errors | `rate(temporal_request_failure[5m]) > 0` | Warning |
