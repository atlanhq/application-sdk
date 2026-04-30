# Incremental Connector Metrics Template

Standard Segment/Mixpanel metrics catalog for any incremental connector.
Sub-agents should use this template to emit consistent, well-labeled metrics
from every incremental connector implementation.

---

## Section 1: Approach

Use the SDK's `get_metrics().record_metric()` directly. No custom sinks,
decorators, or broadcasters needed. All metrics flow through the existing
observability pipeline to Segment and Mixpanel.

```python
from application_sdk.observability.metrics_adaptor import get_metrics, MetricType

CONNECTOR = "my_connector"  # Replace with actual connector name

def emit(name: str, value, metric_type=MetricType.GAUGE, **extra_labels):
    """Emit a single metric with standard labels."""
    labels = {"send_to_segment": "true"}
    labels.update(extra_labels)
    get_metrics().record_metric(
        name=f"{CONNECTOR}_{name}",
        value=value,
        metric_type=metric_type,
        labels=labels,
    )
```

### Rules

- All metric names MUST be prefixed with `{connector}_` for namespace clarity.
- Always include `send_to_segment: "true"` in labels.
- Use `MetricType.GAUGE` for point-in-time values, `MetricType.COUNTER` for
  cumulative totals that only increase within a run.
- Emit metrics as close to the data source as possible (right after the
  computation, not deferred to the end).

---

## Section 2: Metrics Catalog

Organized by emission point. Replace `<entity>` with the actual entity type
name (e.g., `dashboard`, `report`, `dataset`).

### Extraction Overview (emit after entity ID collection)

| Metric Name | Type | Description |
|-------------|------|-------------|
| `total_<entity>_count` | gauge | Total entities in scope for this run |
| `total_field_ids` | gauge | Total child IDs to extract (if applicable, e.g., columns, fields) |
| `is_incremental` | gauge (0/1) | Whether this run used incremental mode |

### Incremental Detection (emit after change detection completes)

| Metric Name | Type | Description |
|-------------|------|-------------|
| `incremental_marker_found` | gauge (0/1) | Whether a prior marker was found in S3 |
| `incremental_updated_count` | counter | Entities with real changes since last marker |
| `incremental_false_positive_skipped` | counter | Entities skipped by false-positive filter (e.g., metadata-only refreshes) |
| `incremental_cascaded_count` | counter | Child entities cascaded from parent changes |
| `incremental_deleted_count` | counter | Entities deleted since last run |
| `incremental_newly_added_count` | counter | Entities newly appearing in filter scope |

### Backfill (emit after backfill completes)

| Metric Name | Type | Description |
|-------------|------|-------------|
| `backfill_record_count` | counter | Total records restored from S3 backfill |
| `backfill_entity_count` | counter | Number of entities backfilled (not freshly extracted) |
| `backfill_duration_seconds` | gauge | Wall-clock time for backfill operation |

### Filter (emit after scope filtering, if applicable)

| Metric Name | Type | Description |
|-------------|------|-------------|
| `filter_before_count` | gauge | Entity count before scope filtering |
| `filter_after_count` | gauge | Entity count after scope filtering |

### Workflow-Level (emit at workflow start)

| Metric Name | Type | Description |
|-------------|------|-------------|
| `workflow_incremental_enabled` | gauge (0/1) | Value of `incremental_enabled` config flag |
| `workflow_force_full` | gauge (0/1) | Value of `force_full_extraction` config flag |
| `workflow_marker_offset_hours` | gauge | Configured prepone buffer in hours |

---

## Section 3: Common Labels

All metrics MUST include these labels in addition to `send_to_segment`:

| Label | Source | Purpose |
|-------|--------|---------|
| `send_to_segment` | Hard-coded `"true"` | Required for Segment routing |
| `connection_qualified_name` | Workflow input / config | Scope key identifying the connection |
| `workflow_id` | Temporal context | Workflow identifier for correlation |
| `workflow_run_id` | Temporal context | Run identifier for deduplication |

### Example emission with all labels

```python
emit(
    "incremental_updated_count",
    len(changed_ids),
    metric_type=MetricType.COUNTER,
    connection_qualified_name=config.connection_qualified_name,
    workflow_id=workflow_info.workflow_id,
    workflow_run_id=workflow_info.run_id,
)
```

### Anti-patterns to avoid

- Do NOT create custom metric sinks or broadcaster classes.
- Do NOT batch metrics to emit at workflow end -- emit at each stage.
- Do NOT use string interpolation for metric values; pass numeric types only.
- Do NOT omit `send_to_segment` -- metrics without it will not reach Mixpanel.
