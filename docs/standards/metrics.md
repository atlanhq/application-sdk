# Metrics Standards

## Core Principles

**Labels must be bounded.** A metric label value must be drawn from a finite,
deployment-time-known set. Anything that scales with users, tenants, requests,
queries, exceptions, or runtime state belongs in **logs**, not in metric labels.

**Diagnostic detail belongs in logs.** The activity.ended log records emitted
by `LogInterceptor` already carry `exception.type`, `exception.message`,
`atlan.exception.cause_chain`, and `atlan.exception.fingerprint` — do not
duplicate that detail as metric labels.

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

## Approved Label Keys

For metrics emitted via `record_metric()` or via the OTel meter, the
canonical bounded label set is:

| Key | Source | Bounded by |
|---|---|---|
| `app_name` | `get_metric_labels()` | `APPLICATION_NAME` constant |
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
