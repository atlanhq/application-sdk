# Workflow Implementation Guide

This reference covers the workflow class setup for incremental extraction.

## Minimal Workflow

For most databases, the workflow class is extremely minimal:

```python
from temporalio import workflow
from application_sdk.workflows.metadata_extraction.incremental_sql import (
    IncrementalSQLMetadataExtractionWorkflow,
)
from app.activities.metadata_extraction.your_db import YourDBActivities


@workflow.defn
class YourDBWorkflow(IncrementalSQLMetadataExtractionWorkflow):
    """Workflow for YourDB incremental metadata extraction."""

    activities_cls = YourDBActivities
```

That's it. The SDK's `IncrementalSQLMetadataExtractionWorkflow` provides:
- `get_activities()` - registers all incremental activities
- `run()` - 4-phase execution
- `_run_incremental_column_extraction()` - parallel batch execution

## Customizing Activity Registration

If your database doesn't support certain entities, override `get_activities()`
and `get_fetch_functions()`:

```python
@workflow.defn
class ClickHouseWorkflow(IncrementalSQLMetadataExtractionWorkflow):
    activities_cls = ClickHouseActivities

    @staticmethod
    def get_activities(activities):
        """Register activities, excluding fetch_procedures (ClickHouse has none)."""
        return [
            activities.preflight_check,
            activities.get_workflow_args,
            activities.fetch_incremental_marker,
            activities.read_current_state,
            activities.fetch_databases,
            activities.fetch_schemas,
            activities.fetch_tables,
            activities.fetch_columns,
            # activities.fetch_procedures,  # ClickHouse has no stored procedures
            activities.transform_data,
            activities.prepare_column_extraction_queries,
            activities.execute_single_column_batch,
            activities.write_current_state,
            activities.upload_to_atlan,
            activities.update_incremental_marker,
            activities.save_workflow_state,
        ]

    @staticmethod
    def get_fetch_functions():
        """Exclude fetch_procedures from the fetch pipeline."""
        return {
            "database": "fetch_databases",
            "schema": "fetch_schemas",
            "table": "fetch_tables",
            "column": "fetch_columns",
            # No "procedure" entry
        }
```

## Workflow Execution Flow

The `run()` method in `IncrementalSQLMetadataExtractionWorkflow` executes:

```
┌─────────────────────────────────────────────────────────┐
│ Phase 1: Setup                                           │
│                                                          │
│  get_workflow_args  →  fetch_incremental_marker           │
│        ↓                         ↓                       │
│  (inject workflow_run_id)  read_current_state             │
│        ↓                         ↓                       │
│              save_workflow_state                          │
├─────────────────────────────────────────────────────────┤
│ Phase 2: Base Extraction (inherited from parent)         │
│                                                          │
│  super().run(workflow_config)                             │
│  → fetch_databases → fetch_schemas → fetch_tables        │
│  → fetch_columns (SKIPPED if incremental)                │
│  → fetch_procedures → transform_data → upload_to_atlan   │
├─────────────────────────────────────────────────────────┤
│ Phase 3: Incremental Column Extraction                   │
│          (only if is_incremental_ready() == True)        │
│                                                          │
│  prepare_column_extraction_queries                        │
│        ↓                                                 │
│  execute_single_column_batch × N  (parallel, max 3)     │
│        ↓                                                 │
│  transform_data (typename="column")                      │
├─────────────────────────────────────────────────────────┤
│ Phase 4: Finalization                                    │
│                                                          │
│  write_current_state  →  update_incremental_marker       │
│  (ancestral merge + S3 upload)                           │
└─────────────────────────────────────────────────────────┘
```

## Retry Policy

The workflow uses:
```python
retry_policy = RetryPolicy(maximum_attempts=3, backoff_coefficient=2)
```

All activities get the same retry policy. Column batch execution uses controlled
concurrency via `MAX_CONCURRENT_COLUMN_BATCHES` (default: 3).

## Concurrency Control

Column batches are executed in groups to avoid overwhelming the database:

```python
# From SDK's _run_incremental_column_extraction
for i in range(0, total_batches, MAX_CONCURRENT_COLUMN_BATCHES):
    chunk = list(range(i, min(i + MAX_CONCURRENT_COLUMN_BATCHES, total_batches)))
    handles = [
        workflow.start_activity_method(
            self.activities_cls.execute_single_column_batch,
            {**workflow_args, "batch_index": idx, "total_batches": total_batches},
            ...
        )
        for idx in chunk
    ]
    all_results.extend(await asyncio.gather(*handles))
```

## main.py Integration

Your `main.py` must register both the workflow and activities:

```python
from application_sdk.server import ApplicationServer

from app.activities.metadata_extraction.your_db import YourDBActivities
from app.workflows.metadata_extraction.your_db import YourDBWorkflow

app = ApplicationServer()

# Register workflow and activities
app.register_workflow(YourDBWorkflow)
app.register_activities(YourDBActivities)

if __name__ == "__main__":
    app.run()
```
