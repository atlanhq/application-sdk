# Metrics Standards

## Core Principles

**Labels must be bounded.** A metric label value must be drawn from a finite,
deployment-time-known set. Anything that scales with users, tenants, requests,
queries, exceptions, or runtime state belongs in **logs**, not in metric labels.

**Diagnostic detail belongs in logs.** The activity.ended log records emitted
by `LogInterceptor` already carry `exception.type`, `exception.message`,
`atlan.exception.cause_chain`, and `atlan.exception.fingerprint` — do not
duplicate that detail as metric labels.

## Understanding cardinality

Authoritative definitions from the
[VictoriaMetrics FAQ](https://docs.victoriametrics.com/FAQ.html):

| Term | Definition |
|---|---|
| **Time series** | A unique combination of metric name + label key/value set. `chunks_written_total{type="pandas",mode="append"}` and `chunks_written_total{type="parquet",mode="append"}` are two distinct series of the same metric. |
| **Active time series** ([VM docs](https://docs.victoriametrics.com/FAQ.html#what-is-an-active-time-series)) | A series that received at least one sample in the last hour. The metric of operational interest — what the TSDB has to keep "hot" in memory for fast lookups. |
| **Cardinality** ([VM docs](https://docs.victoriametrics.com/FAQ.html#what-is-high-cardinality)) | The total number of distinct active time series. "High cardinality" is when this number is high enough to cause memory pressure or slow inserts. |
| **Churn rate** ([VM docs](https://docs.victoriametrics.com/FAQ.html#what-is-high-churn-rate)) | The rate at which old series are replaced by new ones — e.g. every pod restart or new release adds a new `instance` label value, retiring the old one. High churn bloats the on-disk index even if the active series count is bounded. |
| **Slow inserts** ([VM docs](https://docs.victoriametrics.com/FAQ.html#what-is-a-slow-insert)) | The symptom: the in-memory active-series cache overflows, the TSDB falls back to disk lookups, write throughput drops. Caused by either high cardinality or high churn. |

### Why the cardinality discipline matters

A single carelessly-introduced label can multiply series count by orders
of magnitude:

- `error="some specific error message with timestamp 2026-05-04T12:00:00 and table foo"`
  — every distinct message is a new series. Over time: thousands of new
  series that never repeat (high churn) and accumulate in the index.
- `workflow_id="<uuid>"` — every workflow run is one series. With 10 K
  workflow runs per day, that's 10 K new series per day per metric.
- `request_path="/api/users/12345/profile"` — without route templating
  (`/api/users/{id}/profile`), every distinct path is a new series.

Each of those exhibits both high cardinality (lots of unique combinations
*right now*) **and** high churn (those combinations stop receiving samples
quickly, get replaced). The TSDB pays twice: once for the active-series
cache pressure, once for the index bloat from churn.

The bounded label discipline below is what keeps an Atlan app's surface
sub-thousand active series — comfortably below any VM tier's slow-insert
threshold — and keeps churn flat across releases.

## Critical Rules

### **Never use exception messages as metric labels**

- **Anti-pattern**: `labels={"error": str(e)}` — the message embeds session
  IDs, table names, file paths, and error codes that vary per occurrence.
  Each unique message creates a new Prometheus time series.
- **Correct pattern**: `labels={"error_type": type(e).__name__}` — bounded
  class name. Useful for distinguishing `OperationalError` from
  `TimeoutError` from `FileNotFoundError`.

```python
# WRONG — cardinality bomb
self.metrics.record_metric(
    name="write_errors",
    labels={"type": "pandas", "error": str(e)},  # explodes
    ...
)

# RIGHT
self.metrics.record_metric(
    name="write_errors",
    labels={"type": "pandas", "error_type": type(e).__name__},
    ...
)
```

### **Never embed user-controlled identifiers in labels**

Forbidden as label values: `workflow_id`, `run_id`, `request_id`, `trace_id`,
`span_id`, `correlation_id`, file paths, table names, schema names, database
names, query text, payload bodies, IP addresses, hostnames of remote services.

These belong in **log records** and **trace attributes**, where the storage
layer can absorb high cardinality without time-series explosion.

`get_metric_labels()` in `observability/utils.py` lists the only
auto-attached labels (`app_name`, `workflow_type`, `activity_type`) — all
bounded by deployment configuration.

### **Label keys must be consistent across emission sites for the same metric**

Two `record_metric(name="X", labels=…)` calls for the same metric name must
use the **same set of label keys**. Different values are fine; different keys
are not — the OTel→Prometheus exporter splits inconsistent schemas into two
metric families with the same name, producing malformed exposition that
Pushgateway rejects with HTTP 400.

```python
# WRONG — same metric name, different label keys → Pushgateway 400
record_metric(name="chunks_written", labels={"type": "pandas", "mode": ...})  # 2 keys
record_metric(name="chunks_written", labels={"type": "output"})               # 1 key

# RIGHT — same key set
record_metric(name="chunks_written", labels={"type": "pandas", "mode": ...})
record_metric(name="chunks_written", labels={"type": "output", "mode": ...})
```

### **Don't double-count failures across SDK layers**

Activity-level failures are already counted by `MetricsInterceptor` as
`temporal_activity_errors_total{exception_type=...}`. Do not add a parallel
counter that increments on the same failure with a different label scheme —
either the new counter adds a dimension that genuinely doesn't exist on
`temporal_activity_errors_total` (e.g. `format=parquet|json`) or it should
not be added at all.

### **Use established metric names; don't fragment them by data format**

Prefer one metric with a `format` label over many parallel metrics:

```python
# WRONG — three near-duplicate metrics
record_metric(name="json_chunks_written", labels={"type": "daft"})
record_metric(name="parquet_write_records", labels={"type": "daft", "mode": ...})
record_metric(name="chunks_written", labels={"type": "pandas", "mode": ...})

# RIGHT — one metric, format as a label
record_metric(name="chunks_written", labels={"format": "json", "engine": "daft", "mode": ...})
record_metric(name="records_written", labels={"format": "parquet", "engine": "daft", "mode": ...})
```

(Note: the SDK currently has the legacy fragmented form. New metrics should
follow the unified pattern.)

## Inline resource enrichment (always present)

Exactly **one** Resource attribute is inlined onto every metric series by
`EnrichedPrometheusMetricReader` (and onto Temporal Rust-core series via
`TelemetryConfig.global_tags`). Don't duplicate it in `record_metric()`
calls — it's added automatically:

| Always-present label | OTel resource source | Bounded by |
|---|---|---|
| `app_name` | `app.name` | Number of connectors |

`sum by (app_name) (rate(...))` works without a `target_info` JOIN.

Everything else lives on `target_info` and is recovered at query time
via a join. This keeps per-series cardinality minimal (only the labels
that genuinely vary by request/dimension end up multiplied) while
preserving the ability to filter or group by deployment metadata:

| Available via `target_info` (join required) | OTel resource source |
|---|---|
| `app_type` | `app.type` |
| `app_version` | `app.version` |
| `app_release_id` | `app.release_id` |
| `app_sdk_version` | `app.sdk_version` |
| `app_release_channel` | `app.release_channel` |
| `k8s_pod_name`, `k8s_domain_name`, etc. | `k8s.*` |

```promql
# Group by release-id at query time, not in storage:
sum by (app_release_id) (
  rate(http_server_request_duration_seconds_count[5m])
    * on(instance) group_left(app_release_id) target_info
)
```

```promql
# Filter / group by release at query time, NOT in storage
sum by (app_release_id) (
  rate(http_server_request_duration_seconds_count[5m])
    * on(instance) group_left(app_release_id) target_info
)
```

## Reserved by Pushgateway (worker mode)

When the worker pushes to a Pushgateway, two labels are added by the
gateway from the push grouping key. **Do NOT pass them as labels in
`record_metric()` calls** — the duplicate would cause Pushgateway to
reject the push with `400 Bad Request` ("label appears in both metric
body and grouping key"):

| Reserved label | Set by |
|---|---|
| `job` | `PushGatewayClient.job` (e.g. `<app-name>-worker`) |
| `instance` | `_default_grouping_key` → `socket.gethostname()` |

## Approved Label Keys

For metrics emitted via `record_metric()` or via the OTel meter, the
canonical bounded label set is:

| Key | Source | Bounded by |
|---|---|---|
| `workflow_type` | `get_metric_labels()` | Number of registered workflow classes |
| `activity_type` | `get_metric_labels()` | Number of registered activity functions |
| `task_queue` | `MetricsInterceptor` | Number of task queues per app (typically 1-2) |
| `temporal_workflow_type` | `MetricsInterceptor` | same as `workflow_type` |
| `temporal_activity_type` | `MetricsInterceptor` | same as `activity_type` |
| `otel_status_code` | `MetricsInterceptor` | `OK`, `ERROR` |
| `exception_type` | `MetricsInterceptor` | Number of exception classes (~30-50 at most) |
| `mode` | `record_metric` callers | `WriteMode` enum (`append`, `overwrite`, …) |
| `type` / `format` | `record_metric` callers | Bounded enumerations of write paths |
| `error_type` | `record_metric` callers | `type(e).__name__` |

Any new label not on this list needs justification: prove it's bounded.

(Note: `app_name` is also automatically present per the inline
enrichment above — don't pass it explicitly in `record_metric()`.)

## Canonical metric inventory

Names emitted by the consolidated SDK surface (all use OTel base units
— seconds for durations, bytes for sizes — and snake_case with the
`_total` suffix on counters):

| Metric | Type | Source |
|---|---|---|
| `chunks_written_total` | counter | `record_metric()` in `storage/formats/` |
| `write_records_total` | counter | same |
| `write_errors_total` | counter | same |
| `temporal_activity_executions_total` | counter | `MetricsInterceptor` |
| `temporal_activity_errors_total` | counter | same (label `exception_type=type(e).__name__`) |
| `temporal_activity_duration_seconds` | histogram | same |
| `temporal_workflow_executions_total` | counter | same |
| `temporal_workflow_duration_seconds` | histogram | same |
| `http_server_request_duration_seconds` | histogram | FastAPIInstrumentor (stable HTTP semconv) |
| `http_server_request_body_size_bytes` | histogram | same |
| `http_server_response_body_size_bytes` | histogram | same |
| `http_server_active_requests` | gauge | same |
| `temporal_*` (Rust-core families) | various | Temporal SDK Rust core, scraped via FastAPI proxy or pushed via `TemporalCoreCollector` |

## User-facing Metrics Shim

The `application_sdk.observability.metrics` module exposes
`create_counter` / `create_histogram` / etc. for app authors. The same rules
apply: labels passed to `.add()` / `.record()` MUST be bounded.

```python
from application_sdk.observability import metrics

requests = metrics.create_counter("myapp.requests", description="…", unit="1")

# WRONG
requests.add(1, {"workflow_id": workflow_id})  # explodes
requests.add(1, {"sql": query_text})           # explodes

# RIGHT
requests.add(1, {"endpoint": "/foo", "status": "ok"})  # bounded
```

## Volume estimate (current SDK surface)

These numbers describe a split-deployment app (1 server pod + 1 worker
pod) running on the SDK's current observability surface. They're
order-of-magnitude — meant for headroom planning and to make the cost
of new metrics visible — and don't replace measuring your own
deployment.

### Per server pod (handler / combined mode)

| Source | Active series |
|---|---|
| FastAPI HTTP server instrumentation | ~30 (templated `http.route`, status code, method) |
| SDK custom metrics (`record_metric()`) | ~5 |
| Temporal Rust core (proxied through `/metrics`) | ~150 |
| `prometheus_client` defaults (`python_*`, `process_*`) | ~20 |
| **Total per pod** | **≈ 200 active series** |

At a 15 s scrape interval that's ~800 K samples per pod per day.

### Per worker pod (split deployment, push to Pushgateway)

| Source | Active series |
|---|---|
| SDK custom metrics + interceptor `temporal_*` (record_metric path) | ~380 |
| Temporal Rust core (via `TemporalCoreCollector` bridge) | ~460 |
| `prometheus_client` defaults | ~20 |
| **Total per pod** | **≈ 840 active series** |

At a 30 s push interval that's ~2.4 M samples per pod per day pushed to
the Pushgateway.

### Per app (one server + one worker, split-deployment)

- **~1,040 active series**
- **~3.2 M samples/day**

### Headroom

- VictoriaMetrics single-node tier handles ~10 M active series with
  sub-second insert latency on 8-core / 32 GB. A single app is at
  ~0.01 % of that ceiling; even a fleet of dozens of comparable apps
  stays well under 1 %.
- Churn from daily releases adds ~1,000 new pod-instance series per
  app per day. With 30-day retention, the index carries ~30 K series
  fingerprints per app — well under the slow-inserts threshold.

### Multi-tenant SaaS fanout vs. cardinality bombs

These two scaling vectors look superficially similar but have very
different cost profiles, and the doc above conflated them in an earlier
draft. Both increase the series count, but only one is a problem.

**Multi-tenant fanout (safe, linear).** Each customer's pod adds a
constant `tenant_id` / `cluster_name` label to every series it emits
— the value is fixed for that pod's lifetime, same shape as the
`app_name` / `app_type` resource enrichment we already inline.
Series count scales linearly with the number of customers running
the app:

  - 100 customers × ~1,040 series/app = ~104 K active series — fine
  - 1,000 customers × 1,040 = ~1 M — still well within VM single-node
  - 10,000 customers × 1,040 = ~10 M — at single-node ceiling, time
    to plan VMCluster sharding, but not a *cardinality bug*

This is the normal SaaS pattern and what VM is built to handle.

**Intra-pod cardinality bomb (dangerous, multiplicative).** A label
whose value varies *within a single pod's lifetime* multiplies the
series count regardless of how many customers you have:

  - `workflow_id` — UUID per workflow run; one pod processing
    1,000 workflows/day adds 1,000 new series per metric per pod per day
  - `request_id`, `correlation_id` — per-request, same shape
  - `error="<full message>"` — per occurrence; embeds session IDs,
    file paths, error codes
  - `query_text`, `path` (raw, not route-templated) — varies per call

These both inflate the active count *and* contribute high churn. A
single such label can take a pod from ~840 active series to hundreds
of thousands — and the storage cost compounds across every customer
running the app.

**Other things that change the math:**

- Doubling the worker replica count doubles the worker active-series
  contribution per customer (worker side dominates the per-app number).
- An app whose `temporal_*` metrics fan out across thousands of
  distinct task queues or build IDs (similar shape to known cardinality
  bugs on the Temporal **server** side) could grow per-pod active
  series into the hundreds of thousands. Worth a separate analysis if
  you see this pattern.

## Review Checklist for Metric-related PRs

- [ ] Every label value is drawn from a finite, deployment-known set.
- [ ] No `str(e)`, `repr(e)`, or `e.args` anywhere in a `labels=` dict.
- [ ] No workflow/run/request/trace/span/correlation IDs in labels.
- [ ] No file paths, table/schema names, query text, or payload bodies in labels.
- [ ] If the metric name appears at multiple emission sites, they all use the
      same set of label keys.
- [ ] Diagnostic detail (full messages, stack traces) goes to logs via
      `logger.error("...", exc_info=True)` — not into metric labels.
- [ ] If a new metric duplicates an existing dimension already covered by
      `MetricsInterceptor` or the activity-end log, justify the addition.

## Reference Incidents

- **Pushgateway 400 from `chunks_written` label-schema mismatch** (2026-04-30) —
  the same metric was emitted from two call sites with different label-key sets
  (`{type, mode}` vs `{type}`). The OTel→Prometheus exporter produced two metric
  families with the same name; Pushgateway rejected the entire push body.
- **`error=str(e)` cardinality bomb in storage/formats writers** (2026-04-30) —
  four sites used the full stringified exception as a metric label. Each unique
  message (embedding session IDs, table paths, error codes) created a fresh
  Prometheus time series. Latent until a chunks-written-related backstop was
  removed; replaced with `error_type=type(e).__name__`.
