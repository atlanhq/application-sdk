# Orchestration (`run()`) Guide

In v3 there is no workflow class to write. `IncrementalSqlMetadataExtractor`
(deprecated, removed in v4.0.0) defines a concrete `run()` that calls your
`@task` methods; each task becomes a Temporal activity automatically, so
there is no `get_activities()` list and no activity registration. (The file
keeps its old name so existing links still resolve.)

## Minimal App

```python
from application_sdk.templates import IncrementalSqlMetadataExtractor


class YourDBExtractor(IncrementalSqlMetadataExtractor):
    sql_client_class = YourDBClient
    ...  # tasks and build_incremental_column_sql — see activities-implementation.md
```

Override `run()` only to change the orchestration structure itself. An entity
your source does not have (for example, stored procedures) needs nothing: the
incremental `run()` never calls a procedures task.

## Execution Flow

```
┌──────────────────────────────────────────────────────────────┐
│ Phase 1: Prerequisites (sequential)                          │
│   fetch_incremental_marker → read_current_state              │
│   (marker.txt)               (CurrentStateStore.probe)       │
├──────────────────────────────────────────────────────────────┤
│ Phase 2: Base extraction                                     │
│   fetch_databases ┐                                          │
│   fetch_schemas   ┴→ fetch_tables → fetch_columns            │
│   (parallel)                        (skipped if incremental) │
├──────────────────────────────────────────────────────────────┤
│ Phase 3: Incremental columns (only if is_incremental_ready)  │
│   prepare_column_extraction_queries                          │
│        ↓                                                     │
│   execute_single_column_batch × N                            │
│   (MAX_CONCURRENT_COLUMN_BATCHES = 10 at a time)             │
├──────────────────────────────────────────────────────────────┤
│ Phase 4: Write state                                         │
│   write_current_state                                        │
│   (build → diff → upload diff → CurrentStateStore.commit)    │
├──────────────────────────────────────────────────────────────┤
│ Phase 5: Update marker (only after the state write)          │
│   update_incremental_marker                                  │
└──────────────────────────────────────────────────────────────┘
```

`IncrementalRunContext.is_incremental_ready()` is true when incremental
extraction is enabled, a marker exists, and the current state is available.

## Concurrency Control

Column batches run in groups so the source database is not overwhelmed. The
group size is `MAX_CONCURRENT_COLUMN_BATCHES` in
`application_sdk.templates.incremental_sql_metadata_extractor`, which is
`10`. (`application_sdk.constants` has a `MAX_CONCURRENT_COLUMN_BATCHES = 3`
that `run()` does not read.)

```python
# Simplified from IncrementalSqlMetadataExtractor.run()
for chunk_start in range(0, prep_result.total_batches, MAX_CONCURRENT_COLUMN_BATCHES):
    chunk_end = min(chunk_start + MAX_CONCURRENT_COLUMN_BATCHES, prep_result.total_batches)
    chunk_results = await asyncio.gather(
        *[
            self.execute_single_column_batch(ExecuteColumnBatchInput(..., batch_index=i))
            for i in range(chunk_start, chunk_end)
        ]
    )
```

## Retries and Timeouts

Each task's timeout comes from its `@task(timeout_seconds=...)`; retries are
the SDK's task defaults. Every infrastructure task is safe to retry: local
state is run-scoped under `{output_path}/incremental/`, and a
`write_current_state` retry after the commit point reports that commit
instead of rebuilding.
