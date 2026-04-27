# ADR-0012: Observability Consolidation

## Status
**Accepted** (2026-04-27 — Linear [ARUN-539](https://linear.app/atlan-epd/issue/ARUN-539))

## Context

The SDK shipped two custom HTTP/observability surfaces and a sprawling
seven-interceptor stack on the Temporal worker:

- Server side — a hand-rolled `MetricsMiddleware` plus a custom Prometheus
  `/metrics` endpoint, recording `http_requests_total` /
  `http_request_duration_seconds` keyed by **raw `path`** (unbounded
  cardinality).
- Worker side — `ExecutionContextInterceptor`,
  `CorrelationContextInterceptor`, Temporal's `TracingInterceptor` (gated),
  `AppVitalsInterceptor`, `OutputInterceptor`, `EventInterceptor`, and
  `TaskFailureLoggingInterceptor`, each with bespoke wiring and ContextVar
  dependencies.
- "Metrics" emitted as App Vitals **structured logs** (with bespoke
  `metric_name` / `dimension` / `app_vitals=true` filter flag) indexed
  downstream in ClickHouse rather than as real OpenTelemetry instruments —
  un-scrapable by Prometheus and not aggregatable like normal metrics.
- Temporal SDK's Rust-core Prometheus endpoint already wired (port 9464) but
  exposed as a second scrape target, undocumented and externally reachable.
- Production supports **Prometheus for metrics, OTLP for logs, no traces yet**.
  Workers in this SDK are short-lived (per-job pods) and Prometheus may
  never scrape them before they exit.

## Decision

### Server side

- Replace `MetricsMiddleware` with `opentelemetry-instrumentation-fastapi`.
  HTTP metrics now use OTel semantic conventions
  (`http.server.duration`, `http.server.active_requests`) with
  **route-templated** `http.route` labels, eliminating the cardinality
  blow-up.
- Rewrite the `/metrics` endpoint to merge two sources into one response:
  the OTel `PrometheusMetricReader` registry and the Temporal Runtime's
  loopback Rust-core endpoint. Operators get **one** scrape target per
  server pod.
- Bind Temporal Runtime to `127.0.0.1:9464` so the second port is not
  externally reachable.

### Worker side — 7 interceptors → 3

- `LogInterceptor` — emits four lifecycle log lines per execution
  (`workflow.started`, `workflow.ended`, `activity.started`,
  `activity.ended`) with OTel semantic-convention attributes; sets the
  `ExecutionContext` and `CorrelationContext` ContextVars; propagates
  `x-correlation-id` across activity / child-workflow boundaries via
  Temporal headers; logs activity failures with full
  `exception.*` / `atlan.exception.*` fields.
- `MetricsInterceptor` — emits real OTel counters and histograms
  (`temporal.workflow.executions`, `temporal.workflow.duration`,
  `temporal.activity.executions`, `temporal.activity.duration`,
  `temporal.activity.errors`) via the global `MeterProvider`.
- `TraceInterceptor` — thin wrapper around
  `temporalio.contrib.opentelemetry.TracingInterceptor`, gated on
  `ATLAN_ENABLE_OTLP_TRACES` (default off, since production doesn't
  yet support traces).
- Product-feature interceptors stay separate — `OutputInterceptor` (UI
  artifacts merged into workflow output) and `EventInterceptor` (v3
  audit / dashboard event bus) are not observability and remain their
  own interceptors gated by their own settings.

### Pushgateway for short-lived workers

- Worker-only deployments push to `ATLAN_PROMETHEUS_PUSHGATEWAY_URL`
  via `PushGatewayClient` (periodic + final-on-shutdown).
- A `TemporalCoreCollector` registered with `prometheus_client.REGISTRY`
  feeds Temporal Rust-core metrics into the push so we don't lose
  scheduling / cache / poller signals on workers.
- Combined deployments (server + worker in one process) leave the pusher
  off — `/metrics` already covers everything via in-process proxy and
  pushing would double-count.
- Multi-pod safety: hostname-keyed grouping prevents cross-generation
  overwrite. Optional `ATLAN_PROMETHEUS_PUSHGATEWAY_DELETE_ON_SHUTDOWN`
  for graceful exits.

### App Vitals

**Deleted.** Replaced by the four lifecycle log lines emitted from
`LogInterceptor`. ClickHouse views filter on the log body / event name
(`workflow.started`, `workflow.ended`, `activity.started`,
`activity.ended`) and project the old "dimension" categorisation
(reliability / efficiency / throughput / performance) at query time
from `otel.status_code`, `temporal.*.duration_ms`, and event count.
Per-event `dimension` and `app_vitals=true` attributes are gone.

### Custom user metrics

New module `application_sdk.observability.metrics` re-exports the
global OTel meter for app authors:

```python
from application_sdk.observability import metrics

counter = metrics.create_counter("myapp.requests", description="...", unit="1")
counter.add(1, {"endpoint": "/foo"})
```

Writes through the same `MeterProvider` as the SDK's interceptors, so
user metrics appear in the FastAPI `/metrics` proxy on servers and ride
along with the Pushgateway push on workers.

### Local parquet sink

The Hive-partitioned `*.json.gz` sink + object-store upload **stays** —
load-bearing for customer-environment archival. Local cleanup was
already in a `finally:` block, so files don't leak even on short-lived
workers. The `DuckDBUI` class (port 4213) is removed; it was a
dev-workstation convenience and unused in production.

### Env var sweep (no backwards compat)

**Removed:** `ENABLE_APP_VITALS`, `ENABLE_PROMETHEUS_METRICS`,
`ATLAN_ENABLE_PROMETHEUS_METRICS`, `ENABLE_OTLP_METRICS`,
`ENABLE_OTLP_TRACES` (consolidated to `ATLAN_ENABLE_OTLP_TRACES`),
`ENABLE_OTLP_WORKFLOW_LOGS`, `OTEL_WORKFLOW_LOGS_ENDPOINT`,
`METRICS_*` (batch / flush / retention / cleanup),
`TRACES_*` (batch / flush / retention / cleanup),
`enable_correlation_interceptor`.

**Added:** `ATLAN_PROMETHEUS_PUSHGATEWAY_URL`,
`ATLAN_PROMETHEUS_PUSHGATEWAY_INTERVAL_SECONDS`,
`ATLAN_PROMETHEUS_PUSHGATEWAY_DELETE_ON_SHUTDOWN`.

## Consequences

### Breaking

- Prometheus dashboards / alerts that reference `http_requests_total`,
  `http_request_duration_seconds`, or App Vitals attribute names
  (`metric_name`, `dimension`, `app_vitals`, `error_cause_chain`) need
  updating to OTel semconv (`http_server_request_duration_seconds`,
  `http_server_active_requests`, `temporal_*_executions_total`,
  `temporal_*_duration_seconds`, `exception.type`,
  `atlan.exception.cause_chain`, etc.).
- ClickHouse `horizon.app_vitals_events` MV needs to filter on log body
  (`workflow.started` / `workflow.ended` / `activity.started` /
  `activity.ended`) instead of `app_vitals="true"`, and its projected
  columns need to read OTel semconv attribute names. The "dimension"
  categorisation moves into the view.
- The `app_vitals.wf.summary` aggregate event (`total_activities`,
  `bottleneck_activity_type`, `circuit_breaker_tripped`, etc.) is gone.
  The same data is derivable at query time from the four per-activity
  events.

### Operational

- One scrape target per pod (`/metrics` on the server,
  Pushgateway-pushed for workers). Operators no longer need a second
  scrape config for port 9464.
- Pushgateway sticky-data sharp edge: hard-killed worker pods leave
  their last-pushed values in the gateway forever. Mitigations:
  hostname-keyed grouping (no overwrite across generations) and the
  optional `ATLAN_PROMETHEUS_PUSHGATEWAY_DELETE_ON_SHUTDOWN` flag for
  graceful exits.

### Sequenced rollout

1. SDK PR ships first.
2. ClickHouse views + Prometheus dashboards / alerts updated to read
   the new attribute / metric names.
3. SDK consumers bump the dependency only after step 2 has merged into
   the environments scraping them.

## Alternatives considered

- **Keep the `MetricsMiddleware` and rename via aliasing.** Rejected —
  doesn't fix the cardinality blow-up and keeps two parallel emitters.
- **Linger-and-scrape on workers** (sleep `scrape_interval + buffer`
  before exiting). Initially picked, but workers are deliberately short
  lived — Pushgateway is the right shape for batch jobs.
- **OTel collector for metrics** (single push pipeline shared with
  logs). Rejected for now: production doesn't yet have an OTel
  collector accepting metrics, and this would make worker startup
  depend on it. Direct Pushgateway push uses the existing TSDB.
- **Dual-emit window for old + new metric / attribute names.**
  Rejected — hard cutover with consumer-side PRs shipping in the same
  release window.
- **Per-process OTel meter for Temporal core metrics** (push via
  Temporal `OpenTelemetryConfig` instead of Prometheus). Rejected for
  the same OTel-collector reason; the loopback Prometheus + bridging
  collector pattern works without new infra.
