# Design: Temporal Metrics to Upstream ObjectStore

**Status:** Draft | **Author:** Gaurav Tiwary | **Date:** 2026-05-12

## Problem

The 5 Temporal business metrics (`temporal.workflow.executions`, `temporal.workflow.duration`, `temporal.activity.executions`, `temporal.activity.duration`, `temporal.activity.errors`) only flow through Prometheus today. In customer infra without Prometheus/Pushgateway/VictoriaMetrics, they're silently lost.

## Approach

Add a second OTel `MetricReader` on the existing `MeterProvider` that writes to ObjectStore as NDJSON.gz. Zero changes to the interceptor or Prometheus path — OTel fans out to all readers automatically.

```
MeterProvider
  ├── EnrichedPrometheusMetricReader (existing, unchanged)
  │     → prometheus_client.REGISTRY → Pushgateway/scrape → VictoriaMetrics
  │
  └── PeriodicExportingMetricReader (new, 60s interval)
        → ObjectStoreMetricExporter → NDJSON.gz → deployment + upstream bucket
                                                     → ingestion job (out of scope)
                                                     → VictoriaMetrics
```

## What Changes

| File | Change |
|------|--------|
| `observability/_objectstore_metric_exporter.py` | **New.** Custom `MetricExporter` — serializes `MetricsData` to NDJSON.gz, uploads via `upload_file()` to both stores. Best-effort: swallows upload errors. |
| `observability/_objectstore_metric_reader.py` | **New.** Factory wrapping exporter in `PeriodicExportingMetricReader` (60s interval, delta temporality). |
| `observability/metrics_adaptor.py` | **~5 lines.** Register the second reader in `_setup_otel_metrics()` when `ENABLE_OBSERVABILITY_STORE_SINK` is true. |

Everything else is unchanged: interceptor, Prometheus enrichment, pushgateway, worker, `/metrics` endpoint.


## ObjectStore Key Layout

```
artifacts/apps/observability/sdr/prometheus-metrics/
  year=2026/month=05/day=12/hour=14/
    {timestamp_ns}_{deployment}_{app}.json.gz
```

Separate `prometheus-metrics/` prefix from existing `metrics/` (which carries `record_metric()` data). Same partition scheme for MDLH compatibility.

## Configuration

No new env vars. Reuses existing gates:

| Env Var | Effect |
|---------|--------|
| `ENABLE_ATLAN_UPLOAD` (default: `false`) | Controls upstream bucket upload |

## Not Covered

- **Temporal Rust-core metrics** (scheduling latency, cache hit rates from `:9464`) bypass OTel entirely — they flow through `TemporalCoreCollector` → `prometheus_client.REGISTRY` only. This design does not capture them.
- **Downstream ingestion** (upstream bucket → VictoriaMetrics) is a separate service, out of scope for the SDK.



