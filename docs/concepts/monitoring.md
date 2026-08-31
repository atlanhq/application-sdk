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
| **Combined** | Direct scrape | `http://<pod>:8000/metrics` (FastAPI, including proxied Temporal Rust-core metrics when enabled) |
| **Handler** | Direct scrape | `http://<pod>:8000/metrics` (FastAPI in-process metrics; Temporal Rust-core proxy disabled by default) |
| **Worker** (split deployment) | Push | Pushgateway at `ATLAN_PROMETHEUS_PUSHGATEWAY_URL` |

The Temporal SDK's Rust-core Prometheus endpoint (`127.0.0.1:9464`) is
bound **loopback only** and is not externally reachable. The FastAPI
`/metrics` handler proxies it in-process for combined pods; handler-only
pods skip that proxy by default because no local worker/runtime is started
with the handler process. The worker's `TemporalCoreCollector` reads it
locally and includes the families in each Pushgateway push. So scrape
configurations always target the FastAPI port (or the Pushgateway), never
`:9464` directly.

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
  histograms emitted by activity/workflow execution) plus, when enabled
  and reachable, the Rust-core families proxied from `127.0.0.1:9464`
  (`temporal_request_*`, `temporal_workflow_task_*`,
  `temporal_sticky_cache_*`, `temporal_num_pollers`, etc.)
- **`prometheus_client` defaults** (`process_*`, `python_gc_*`,
  `python_info`)

### `ATLAN_ENABLE_TEMPORAL_CORE_METRICS`

This env var **does not gate the FastAPI `/metrics` route** — that
endpoint is always registered. The flag controls whether worker/combined
mode creates a Temporal Runtime that binds `127.0.0.1:9464`:

| Value | Effect |
|---|---|
| `true` (default) | Rust core binds 9464 in worker/combined mode; combined FastAPI proxy + worker `TemporalCoreCollector` can read it |
| `false` | Rust core builds an explicit `Runtime` with log forwarding but no Prometheus listener; FastAPI `/metrics` still serves SDK + HTTP + python defaults but lacks the `temporal_*` Rust-core families |

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

### Stall-watchdog metrics

The in-process stall watchdog (ADR-0018) emits two histograms, and they have **two
different audiences**: an app author reading their own work-list, and an operator being
paged. See [Progress and Stalls](progress-and-stalls.md#reading-your-warn-report) for the
author-side queries.

| Metric | Records | Labels | Read by |
|---|---|---|---|
| `task_no_progress_gap_seconds` | one entry per gap that exceeded `max_no_progress_seconds` | `task_name`, `progress_last_label`, `watchdog_mode` | The author's work-list **and** the `AtlanAppTaskStalled` alert — while an app is in `warn`, this is the containment |
| `task_hold_duration_seconds` | one entry per hold released — every hold, not only long ones | `task_name`, `hold_label`, `hold_bounded`, `hold_lapsed` | The author's work-list only — deliberately not alerted |

`watchdog_mode` is what keeps warn-mode observations and enforced kills aggregating
separately; `hold_bounded="false"` isolates the sites still relying on the duration
backstop.

The accompanying log lines are **INFO by design**, not WARNING: warn mode is a
fleet-wide default, so one gap observation is an expected observation rather than an
actionable failure, and alerting off the log level would manufacture exactly the
fleet-wide noise ADR-0018 exists to reduce. **The alert reads the metric**, and it is
deliberately not one-gap-sensitive — see the threshold below.

- **Alert rule:** `AtlanAppTaskStalled` in
  [`atlanhq/atlan-alerts`](https://github.com/atlanhq/atlan-alerts/blob/main/alerting/rules/App-Platform/atlan-apps-task-stall-alerts.yaml).
- **Runbook:** [Stalled task](../runbooks/stalled-task.md) — how an operator tells a
  wedge from a healthy-but-quiet spot, and what to do with each.
- **Dashboard:** import
  [`task-stall-dashboard.json`](../static/observability/task-stall-dashboard.json)
  into Grafana against the Prometheus/VictoriaMetrics datasource.

!!! warning "Split-deployment workers need the Pushgateway to be configured"

    Activities run in the worker process, so in a split deployment these series only
    reach VictoriaMetrics if `ATLAN_PROMETHEUS_PUSHGATEWAY_URL` is set. With it unset the
    worker logs a warning at startup and pushes nothing — no task-level series exists for
    that app, so neither the work-list nor the stall alert works for it.
    Combined-mode pods are covered by the FastAPI `/metrics` scrape.

### Recommended Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| High activity failure rate | `rate(temporal_activity_execution_failed_total[5m]) > 0.05` | Warning |
| Worker slots exhausted | `temporal_worker_task_slots_available == 0` | Critical |
| Elevated workflow latency | `histogram_quantile(0.99, temporal_workflow_endtoend_latency_bucket) > 300` | Warning |
| Temporal server errors | `rate(temporal_long_request_failure_total[5m]) > 0` | Warning |
| Task stalled (wedged but alive) | `AtlanAppTaskStalled` — see below | High |
| Run past its length SLA | `increase(task_run_length_over_sla_seconds_count[15m]) > 0` — see below | Warning |

The stall alert is **not optional while an app is in `warn`.** Warn mode cannot fail an
activity, so an alert on this metric is what stands in for the kill: a wedged attempt is
visible within `max_no_progress_seconds`, but it will hold its worker slot until the 24h
backstop unless a human intervenes. Once an app runs in `enforce`, the same series
measures its kill rate instead.

It is the one alert here that ships as a real rule rather than a suggestion —
`AtlanAppTaskStalled` in `atlanhq/atlan-alerts`, with the
[stalled-task runbook](../runbooks/stalled-task.md) behind it:

```promql
sum by (clusterName, domainName, app_name, task_name,
        progress_last_label, watchdog_mode) (
  increase(task_no_progress_gap_seconds_sum{app_name!="", task_name!=""}[1h])
) >= 3600
```

**Seconds of silence per hour, not a count of gaps** — and the difference matters. In
warn mode *every* app with an uninstrumented quiet spot emits gaps; that is what warn
mode is for, and paging each one would manufacture the fleet-wide noise ADR-0018 exists
to reduce. Only *sustained* silence separates a wedge from a healthy-but-quiet spot,
because both emit nothing and only the wedge keeps emitting nothing. `3600` is one
permanently-silent attempt, and N concurrent wedges reach it N times faster — so the page
arrives soonest exactly when worker-slot exhaustion is the real risk. A single gap belongs
on the dashboard and in the app's own work-list, not in an operator's inbox.

### Run length: the run no stall and no timeout catches

The stall alert above catches an attempt that goes *silent*. It cannot catch a run
that keeps making **some** progress: every progress signal re-arms the watchdog, so
no gap is ever observed, and with `start_to_close` raised to a backstop no timeout
fires either. `temporal_workflow_duration_seconds` does not help — it is recorded
when a run *ends*, so it can never describe a run that has not ended. That run is
bounded by nothing, which is why ADR-0018 → *Bounding total time* replaces the
duration **kill** with a duration **alert**.

Every executing task attempt compares its run's age against
`ATLAN_RUN_LENGTH_SLA_SECONDS` (24 h by default; `0` disables) on the same tick as
its heartbeat. Past the SLA it emits:

| Signal | Shape |
|---|---|
| `task_run_length_over_sla_seconds` | Histogram of the run's age, labels `task_name`, `temporal_workflow_type`. Recorded **only** while a run is over its SLA, and re-asserted once a minute for as long as it stays over |
| One `WARNING` log line per attempt | The age, the SLA, the task that was running and the workflow type |

```promql
increase(task_run_length_over_sla_seconds_count[15m]) > 0
```

**A count, here, where the stall alert needs accumulated seconds** — and the
asymmetry is the point. Gaps are emitted by every warn-mode app with an
uninstrumented quiet spot, so only sustained silence distinguishes a wedge from a
healthy pause. This series is emitted *only* by a run already past the length its
own operator declared, so there is nothing to threshold: one observation is the
finding. `histogram_quantile` over `_bucket` then answers "how far over". Because
the observation re-asserts every minute, the window only has to exceed a minute —
`[15m]` leaves slack and resolves within one window of the run ending.

**Runbook:** [Long-running run](../runbooks/long-running-run.md) — how an operator
separates healthy-but-slow from wedged, and what to do with each.

!!! warning "Same Pushgateway requirement as the stall metric"

    This series is emitted from the worker process, so in a split deployment it
    only reaches VictoriaMetrics if `ATLAN_PROMETHEUS_PUSHGATEWAY_URL` is set.

**Blind spot, stated.** The observation rides an activity's heartbeat tick, so a
run is measured only while at least one of its tasks is executing. A run parked
between activities, a run whose worker is gone entirely, and a task with
heartbeating disabled are not covered — those are "no worker / no activity"
conditions, and the worker-liveness and activity-failure alerts above are what see
them.

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
| `app_name` | The running entry-point's app name (see note below) |
| `workflow_run_id` | Temporal workflow run ID (canonical name; `run_id` is a backwards-compat alias) |
| `correlation_id` | Platform-level correlation identifier |
| `source` | Which layer emitted the line — `sdk`, `dependency`, or the app's own name (see [Log provenance](#log-provenance-the-source-field)) |

These fields appear on every log entry without any manual binding — use `self.logger` directly.

> **`app_name` is per-entry-point (CNCT-93).** It is resolved from the workflow's
> own input args, which the contract toolkit stamps with each DAG node's own
> `app_name`. For a single-entry-point connector this is just its kebab-case name
> (e.g. `mysql`). For a **multi-entry-point bundle** it is the *entry-point's*
> name — `powerbi-crawler`, `powerbi-miner` — **not** the connector-level bundle
> name, so each entry-point's logs are queryable on their own. This is the value
> the run UI queries a node's logs by. When the workflow input carries no
> `app_name` (an older, not-yet-regenerated app), it falls back to the process-wide
> `ATLAN_APPLICATION_NAME` env — i.e. prior behaviour. The per-entry-point value
> is a **log-only** field: metrics (both the `temporal_*` lifecycle families and
> `record_metric()` custom metrics) and the trace-level `app.name` (OTel
> Resource) deliberately stay **connector-level**, so for a bundle a metric's or
> trace's `app_name` may differ from the `app_name` on its correlated log lines
> — by design. See [Metrics Standards](../standards/metrics.md).

```python
class MyConnector(App):
    @task
    async def fetch_data(self, input: FetchInput) -> FetchOutput:
        self.logger.info("fetching page=%d", page_num)
        # Emits: {"level":"INFO","msg":"fetching page=3","app_name":"my-connector","workflow_run_id":"...","correlation_id":"..."}
```

Use **%-style** message bodies (`"fetching page=%d", page_num`) rather than keyword arguments. See [Logging Standards](../standards/logging.md) and [ADR-0011](../adr/0011-logging-level-guidelines.md).

### Structured attributes and the OTLP allowlist

Structured kwargs on a log call (e.g. `logger.info("event", outcome="clean", assets_total=42)`) are
not forwarded to OTLP wholesale. Only keys on an SDK-side **allowlist**
(`_KNOWN_EXTRA_KEYS` in `application_sdk/observability/logger_adaptor.py`) become `LogAttributes` on
the exported record; unlisted keys are dropped. This keeps the exported attribute set intentional —
adding a new queryable field is a deliberate one-line change next to the emitter. Certain dotted
prefixes (`atlan.`, `temporal.`, `failure.`, `exception.`, `otel.`, `tenant.`, `workflow_run.`) pass
through without being individually listed.

### Log provenance: the `source` field

Every record carries a low-cardinality `source` attribute answering *which layer emitted this line*.
It is stamped centrally — by both `AtlanLoggerAdapter` and the stdlib-logging bridge — so app authors
cannot get it wrong, and it is allowlisted for OTLP, so it is queryable alongside `app_name`:

| `source` | Records it covers |
|----------|-------------------|
| `sdk` | Anything logged from inside `application_sdk` — framework internals and the interceptor lifecycle lines |
| `dependency` | Known third-party loggers (`httpx`, `daft_io`, `temporalio`, …) and the forwarded `daprd` sidecar lines |
| *the app label* | Everything else — by default the app's own `APPLICATION_NAME` (e.g. `mysql`), so a reader sees *which* app spoke |

`source` answers a different question from `app_name`: `app_name` says *which app / entry-point* the
record came from (per-entry-point for a bundle — see the note above), `source` says *which layer
within it*. That makes `source = sdk` the filter for "is this the framework or the connector's own
code" during triage.

The app label is overridable with the `ATLAN_LOG_SOURCE` environment variable — the Automation
Engine sets `ATLAN_LOG_SOURCE=ae` so its orchestration lines are attributable to the engine rather
than to whichever app it happens to be running. Apps should leave it unset. See
[Common Utilities → Logging Configuration](common.md#configuration).

An explicit `source=` kwarg on an individual log call still wins over the derived value.

### Log↔trace correlation (`trace_id` / `span_id`)

OTLP log records carry real trace context: `trace_id` and `span_id` are populated from the span
active in the emitting thread, so a log line joins to its span in the trace UI. Previously both were
hard-coded to zero, which made log↔trace correlation impossible.

When no span is active — which includes the case where trace export is off
(`ATLAN_ENABLE_OTLP_TRACES` unset, the current production default) — both fall back to zero and the
record is exported untraced, exactly as before. Enabling traces is therefore what turns this
correlation on.

`correlation_id` remains the business-level join key and is **not** replaced by trace context: it
spans an entire platform-level operation across process and workflow boundaries, whereas a
`trace_id` covers one trace. See [Correlation IDs](#correlation-ids).

### Lifecycle log lines

`LogInterceptor` emits four lifecycle lines per run, with OTel semantic-convention attributes
attached. The **event token is the exact message prefix** — that is the stable, greppable contract
for dashboards and alerts:

| Token | Level | Message body |
|-------|-------|--------------|
| `workflow.started` | INFO | `workflow.started <WorkflowType>` |
| `workflow.ended` | INFO / WARNING / ERROR | `workflow.ended <WorkflowType> OK (<ms>ms)`, `… BLOCKED (preflight gate)`, or `… FAILED (<code>): <message> — at <file>:<line> in <fn>` |
| `activity.started` | INFO | `activity.started <ActivityType>` |
| `activity.ended` | INFO / WARNING / ERROR | same three shapes as `workflow.ended` |

The body after the token is a **human-readable summary, not a contract** — it names the subject and,
on failure, folds in the first line of the exception message (truncated) and the root-cause frame, so
a line stays diagnosable in renderers that drop structured attributes. Match on the token prefix and
the structured attributes, never on the body text.

!!! note "Token stability"

    The task-level tokens (`activity.started` / `activity.ended`) and workflow-level tokens
    (`workflow.started` / `workflow.ended`) are a stable contract — dashboard queries, saved log
    searches, and alert rules may match on these literal prefixes. The bodies after the token are
    human-readable summaries and may change; match on the token prefix and the structured
    attributes, not the body text.

### Asset-validation outcome event

`App.upload()`'s warn-only asset validation (see [Apps → Asset-Validation Outcome](apps.md#asset-validation-outcome))
emits a structured event named `"Transformed-asset validation outcome"` on **every validated
upload**, from inside the `upload` activity — so the Temporal context (`workflow_run_id`, `app_name`)
is auto-stamped and each row joins to the workflow outcome by run id in ClickHouse. The following
attributes are allowlisted and reach OTLP:

| Attribute | Meaning |
|-----------|---------|
| `outcome` | `"clean"` or `"flagged"` |
| `assets_total` | records seen in the batch |
| `assets_passed` | records that passed per-asset `.validate()` |
| `assets_invalid` | per-asset validation failures |
| `assets_orphaned` | referential-integrity (orphan) failures |
| `assets_undeserializable` | records that could not be decoded |
| `asset_validation_matrix` | compact JSON array of per-failure detail (bounded rows per axis), `JSONExtract`-able |

Emitting `outcome="clean"` too gives a denominator, so a dashboard can rank connectors by
flag-rate rather than only seeing failures. Uploads with nothing to validate (validation disabled, or
a non-`transformed/` path) emit no event.

Since [ADR-0020](../adr/0020-artifact-validation.md) this check is the artifact wrapper's
NDJSON × `ModelSource` cell, reached as `validate_artifact(target, ModelSource(model=Asset))`. **The
event above is unchanged by that** — name, keys, `outcome` vocabulary and matrix row keys are all a
shipped contract, and the fold-in was a refactor behind it. One hand-off emits one row, so this
upload does not additionally emit `"Artifact validation outcome"`; the two events are two
vocabularies, not two checks.

### Artifact-validation outcome event

The generic artifact-validation wrapper ([ADR-0020](../adr/0020-artifact-validation.md)) emits
`"Artifact validation outcome"` — one row per artifact hand-off, whatever the format and whichever
schema source declared it.

Rows are emitted automatically by the activity interceptor. Every `@task` in every app funnels
through one seam, and both enforcement points come off the one declaration there:

```
  materialize_file_refs(...)   # durable -> local
+ validate(ingest)             <- consumer side, re-validate on read
  result = await task_method(input_data)
+ validate(handoff)            <- producer side, BEFORE persist, while the bytes are still local
  persist_file_refs(...)          so a flag blames the producer, not whoever reads it three hops on
```

The interceptor resolves declarations from the app's generated contract, so every row it emits
carries `artifact_schema_source=contract` — NDJSON checked record by record, parquet by diffing the
file footer with no row read. The `model` source is reached the other way, from the upload hook's
asset cell, and that hand-off emits the *asset* event above rather than this one: one hand-off, one
row. The single cell that still answers `unsupported` is parquet x `ModelSource` — a model carries no
column mapping, so a footer diff would have nothing to diff against, and it says so out loud rather
than going quiet.

Whether a negative outcome **blocks** is the app's own posture, and the default is that it never
does — see [Artifact-validation posture](#artifact-validation-posture) below. Set
`ATLAN_VALIDATE_ARTIFACTS=false` to turn the whole hook off for a deployment — note this stops the
outcome events too, so an app then has no denominator at all.

| Attribute | Meaning |
|-----------|---------|
| `outcome` | `clean`, `flagged`, `not_declared`, `unsupported` or `absent` |
| `reason` | short explanation, chiefly for the three non-scan outcomes |
| `artifact_format` | `ndjson`, `parquet` — empty when nothing was read |
| `artifact_schema_source` | `contract` or `model` |
| `artifact_field` | output-contract field the artifact came from; with `entrypoint` this keys the declaration |
| `artifact_side` | `ingest` (consumer side, after materialise) or `handoff` (producer side, before persist) |
| `artifact_unit` | what the counts count: `record` (streaming scan) or `column` (footer diff) |
| `artifact_total` | units examined — always the whole artifact, never a sample |
| `artifact_passed` | units with no failure |
| `artifact_failed` | units that failed |
| `artifact_undecodable` | units that could not be parsed at all |
| `artifact_fields_declared` | fields the declaration named (0 for a model declaration) |
| `boundary` | whether the hand-off sits on an entrypoint's public interface |
| `artifact_classification` | `verdict`, `artifact_unverifiable` or `validator_broken` — see below |
| `artifact_validation_mode` | the app's resolved posture: `hard` or `soft` |
| `artifact_enforcement` | `blocked`, `would_block`, or empty when the outcome was never blockable |
| `artifact_validation_matrix` | compact JSON array of per-failure detail (bounded rows), `JSONExtract`-able |

Two properties are worth relying on. **Every hand-off emits**, the negative outcomes included — a
check that reports nothing is indistinguishable from a check that passed, so `not_declared`,
`unsupported` and `absent` are rows, not silence. And **every attribute is present on every
outcome**, the matrix as `"[]"` when there is nothing to show, so a consumer parses it
unconditionally instead of branching on presence.

`boundary` is what makes `not_declared` actionable. An entry point's `input_type`/`output_type` are a
public interface, so a missing declaration there is a finding against the app; the same gap on an
internal `@task` contract is informational. The set of boundary contract classes is resolved once at
worker build, so the attribute costs nothing per hand-off.

The event body and its attribute keys are a pinned contract, like the three preflight events and
the asset-validation one; all six names live in `application_sdk/observability/events.py`.

### Artifact-validation posture

An app declares whether an artifact-validation outcome may fail the activity, exactly as it does for
the preflight gate:

```python
class MyApp(App):
    artifact_validation_mode = "hard"   # default: "soft"
```

`ATLAN_ARTIFACT_VALIDATION_MODE` overrides the declaration at deploy time, so a fleet that starts
flagging can be stood down without an app release. **Only the literal `hard` enforces** — a typo, a
`true`, an unset variable all resolve to soft, so a run is never blocked by accident. Both the
attribute and the variable are read **once at worker build** and baked into the activity closure, so
a worker's blocking behaviour cannot change under a running activity.

In hard mode a blockable outcome raises `ArtifactValidationBlockedError`, which the activity path
translates into a non-retryable `ApplicationError` carrying `FailureDetails` — the field, the side
and the declared-vs-found detail land in the red activity pane without anyone parsing a message. In
soft mode the identical outcome emits `artifact_enforcement="would_block"` and the hand-off
continues. Both values come off one call on one report, so the soft rows are an exact forecast of
what hard mode would have done: that is what makes graduating a measured decision.

`artifact_classification` is the second axis, and it is what keeps hard mode safe:

| Classification | Meaning | Subject to posture? |
|----------------|---------|---------------------|
| `verdict` | a scan ran and answered | yes |
| `artifact_unverifiable` | nothing on the SDK's side broke; there was nothing to check, or nothing to check against | yes |
| `validator_broken` | the SDK's own plumbing failed — a plug-in raised, a declaration file was unreadable | **no, always fails open** |

Every plumbing failure degrades to `outcome=absent`, which it shares with the honest "the artifact
was not there", so the outcome alone cannot separate them — the classification can, and only one of
the two is ever allowed to block. One more outcome is exempt: `not_declared` off a public boundary
never blocks, because declaration is deliberately optional on app-internal `@task` contracts.

To find every hand-off a hard-mode graduation would break, without graduating:

```sql
SELECT LogAttributes['app_name'] AS app,
       LogAttributes['artifact_field'] AS field,
       LogAttributes['artifact_side'] AS side,
       count() AS n
FROM otel_logs.service_logs
WHERE Body = 'Artifact validation outcome'
  AND LogAttributes['artifact_enforcement'] = 'would_block'
GROUP BY app, field, side
ORDER BY n DESC
```

### Artifact-validation posture event

`"Artifact validation posture"` fires **once per registered app at worker build** — soft apps and
switched-off deployments included. It carries `app_name` and `artifact_validation_mode`
(`hard`/`soft`/`off`, where `off` means `ATLAN_VALIDATE_ARTIFACTS` is down for that deployment).

It exists because the outcome events cannot supply a denominator. An app whose tasks hand off no
artifacts, or whose worker never runs one, emits no outcome row at all — so from outcomes alone a
hard-mode app that has never blocked anything is indistinguishable from one that is not registered.
This row is what makes adoption and posture drift measurable rather than a code-search artifact.

### Replay suppression

`self.logger` suppresses log output during Temporal workflow replay by default, matching the behaviour of Temporal's native `workflow.logger`. This prevents duplicate bare lines (missing `workflow_id`/`run_id`) that would otherwise appear in Grafana when a worker replays history after a sticky-cache eviction.

If you need replay logs — for example when using `temporalio.worker.Replayer` locally to inspect workflow history — re-enable them in two ways:

- **Per-instance** (in-code): `self.logger.log_during_replay = True`
- **Globally** (env flag): `ENABLE_WORKFLOW_REPLAY_LOGS=true`

---

## Memory pressure and OOM diagnostics

OOM kills produce no application-level log — the kernel sends SIGKILL and the
process vanishes silently. The SDK surfaces four log signals so operators have
a clear evidence trail and a short time-to-diagnosis.

### Log surfaces (grep patterns)

| # | When | Level | Where | Grep |
|---|------|-------|-------|------|
| 1 | Worker/combined/handler startup | `INFO` | pod log (first lines) | `"Process memory at start"` |
| 2 | Every 20 s during an active task, once RSS ≥ 80 % of limit | `WARNING` | pod log | `"Memory pressure on task"` |
| 3 | Immediately after pod restart, if `exitCode == 137` | `CRITICAL` | pod log (first lines of new pod) | `"exit code 137 = SIGKILL"` |
| 4 | When Temporal re-dispatches an activity after worker loss | `WARNING` | workflow log | `"re-dispatched after worker eviction"` |

Signal 1 establishes a baseline (RSS at startup, limit, %) so you can see
where memory stood when the pod was last healthy. Signal 2 fires on the rising
edge and re-arms after the ratio drops below 75 %, giving pre-kill leading
indicators in the killed pod's log. Signal 3 fires in the **replacement** pod's
entrypoint immediately on restart — before any Temporal heartbeat timeout — so
the first thing you see in `kubectl logs` is the exit code, along with the
diagnostic commands to run. Signal 4 names OOM kill (pod exit 137) explicitly
alongside KEDA scale-down, spot preemption, and rolling deploys.

### Required Kubernetes configuration

Signal 2 and signal 1's percentage require `K8S_POD_MEMORY_LIMIT` to be
injected via the Downward API:

```yaml
env:
  - name: K8S_POD_MEMORY_LIMIT
    valueFrom:
      resourceFieldRef:
        resource: limits.memory
        divisor: "1"          # raw bytes; parse_pod_memory_limit() also accepts Ki/Mi/Gi suffixes
```

When this env var is absent the memory-pressure warning is silently disabled
(no false positives in local dev / non-Kubernetes environments).

### Diagnostic runbook (OOM kill)

1. **Check exit code in the new pod** (`kubectl logs <pod>` — look for the exit-137 CRITICAL line at the top, or the process memory at start line showing a high baseline).
2. **Confirm OOM kill** — `kubectl describe pod <pod>` → `lastState.terminated.reason: OOMKilled`.
3. **Check cluster events** — `kubectl get events -n <namespace> --field-selector=reason=OOMKilling`.
4. **Review memory trajectory** — search the killed pod's log for `Memory pressure on task` lines to see how fast RSS climbed before the kill.
5. **Check eviction retries** — search the workflow log for `re-dispatched after worker eviction` to understand how many attempts Temporal made.

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

By default, logs, metrics, and traces are also written to gzip-compressed NDJSON files (`.json.gz`) in the object store under `artifacts/apps/{app_name}/{deployment_name}/observability/`. This enables historical querying even when the live pipelines (OTLP for logs/traces, Prometheus scrape / Pushgateway push for metrics) are unavailable.

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

---

## Activity Sizing Telemetry

Records what one activity execution actually consumed — peak container memory, CPU time and throttling, input bytes, duration, outcome — so pod sizing can be derived from measurements instead of guesswork. Collection only: nothing in this path reads or changes how an activity is routed.

**Off by default.** A version bump alone changes nothing, and being enabled is not on its own enough — the allow-list must name the activities to measure:

```bash
APPLICATION_SDK_ENABLE_SIZING_TELEMETRY=true          # master switch (default: false)
APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES=merge     # comma-separated; "*" for all
APPLICATION_SDK_SIZING_TELEMETRY_POLL_SECONDS=1.0     # peak-memory poll interval
```

The allow-list is **fail-closed**: with the switch on and the list empty, nothing is collected and the worker logs a warning at startup saying so. Names match either the bare task name (`merge`) or the qualified form (`my-app:merge`).

Attached as a Temporal `ActivityInboundInterceptor`, so an app is covered without changing any code — but only for the activities it names. Measure the ones whose resource use varies with their data; rows from fixed-cost activities add nothing to the dataset a tier table is fitted from.

### What a row contains

One row per activity execution, emitted two ways: six OTel histograms under `activity.sizing.*` for dashboards, and a `sizing` prefix in the object store (see [Observability Store Sink](#observability-store-sink)) plus one `activity_sizing_observation` JSON log line for offline fitting.

Four fields decide how a row may be used:

| field | why it matters |
|---|---|
| `peak_delta_bytes` | Peak **above** the memory already resident when the activity started. Fit multipliers on this. `memory.current` is pod-wide and cumulative, and allocators do not return freed pages promptly, so the Nth activity in a pod starts from whatever the (N−1)th left pooled — pooling absolute peaks across pods with different histories fits the slope to that carryover as much as to the workload. |
| `peak_memory_bytes` | The absolute peak. **Provision on this**, because it is the number the OOM killer compares. Both travel together for that reason. |
| `is_attributable` | `False` means the peak is pod-wide rather than this activity's, because other activities shared the process (`concurrency_max > 1`). Still useful — for fitting a *pod* envelope, not an activity's. Written out rather than left to be derived, since a consumer that forgets it pools two different quantities. |
| `peak_source` | `watermark` (kernel high-water mark, catches a spike of any duration), `poll` (background sampling, blind to sub-interval spikes), or `unavailable` (no cgroup). A tier fitted to a silent mix of the first two is fitted to an unknown error profile. |

`input_bytes` is the driver variable: peak memory alone says a tier is wrong, but not what to key it on. The SDK's own file readers report it automatically; an app fetching data another way calls `report_input_bytes()` directly:

```python
from application_sdk.observability.sizing_inputs import report_input_bytes

report_input_bytes(len(payload))
```

Every reader returns `None` rather than a guessed `0` — a missing reading has to stay distinguishable from a real zero, or sizing picks the smallest tier. Nothing is emitted at all unless a cgroup reading was obtained, so on a host without one (local macOS/Windows, or a pod with no memory controller) no rows are produced.

### Batching and shutdown

Rows are buffered and flushed on a timer as well as on batch size, because these workers commonly handle a handful of activities and then scale to zero — a size-only trigger would leave a pod's whole buffer unflushed. **Worker shutdown flushes the last batch**, which on a low-rate workload is otherwise most of the data.

```bash
ATLAN_SIZING_BATCH_SIZE=500
ATLAN_SIZING_FLUSH_INTERVAL_SECONDS=60
ATLAN_SIZING_RETENTION_DAYS=90
```

This signal writes to the object store even when `ATLAN_ENABLE_OBSERVABILITY_STORE_SINK` is off. That switch covers logs, metrics and traces together, and an app disabling it to stop shipping those would otherwise silently lose a dataset it explicitly opted into — with an empty prefix as the only symptom. Collection is already gated twice, by the switch and the allow-list, so nothing is written unless an operator asked for it by name.

### Cost

About four file reads per activity plus two per poll tick, and no RPC — all local `/sys/fs/cgroup` reads. Telemetry never fails the activity it measures: every failure path costs the observation, not the activity's real outcome, and a failed shutdown flush does not block shutdown.

---

## Forwarding daprd Sidecar Logs

The `daprd` sidecar's own logs go straight to the container's stdout/stderr and don't enter the SDK observability pipeline on their own.

- **On atlan-infra** (`ENABLE_ATLAN_UPLOAD=false`) this is fine: a node-level filelog collector scrapes every container's stdout — daprd included — and ships it to the central log backend.
- **In SDR mode** (`ENABLE_ATLAN_UPLOAD=true`, i.e. customer infra) there is **no** such node-level collector, so daprd's logs would be invisible to Atlan — only present in the customer's own pod logs.

So in SDR mode the SDK **automatically** forwards daprd's logs through its own pipeline (the same path as the app's logs, so they land in the lakehouse `app_logs` table with `app_name` / `is_sdr` populated), while still echoing them to the container's own logs. No configuration is required — it is gated on `ENABLE_ATLAN_UPLOAD`. Under the hood the container entrypoint runs daprd under `application_sdk.observability.dapr_log_forwarder`, which streams each daprd log line into a `dapr.runtime` logger and forwards `SIGTERM` so graceful shutdown is unaffected.

> **Note (`kubectl logs` format in SDR mode):** In SDR mode daprd's stdout/stderr is piped into the forwarder, so `kubectl logs` shows the SDK's re-emitted format (with level, `app_name`, timestamp) rather than daprd's raw `--log-as-json` lines. The content is the same; only the format differs.

daprd is chatty, so control the volume at the source with its log level:

```bash
DAPR_LOG_LEVEL=warn   # forward warn + error (and above); raise to error to drop the rest
```

`DAPR_LOG_LEVEL` is a minimum-severity floor — `warn` captures both warnings **and** errors. It's the recommended knob for controlling daprd log volume reaching the lakehouse.
