# Analysis Scripts

Performance analysis and optimization experiments for Iceberg workflow logs.

## Scripts

### `partition_analysis.py`

Analyzes the impact of partitioning strategy when partition key differs from query predicate.

**Key Insight:** Partitioning by `atlan_workflow_name` but querying by `trace_id` won't benefit from partition pruning - even if the values are identical.

### `partition_pruning.py`

Explains Iceberg's partition pruning behavior with examples.

**Key Findings:**
- Predicate pushdown only works on partition columns
- No automatic column equivalence detection
- Query planner can't correlate different columns

### `performance_demo.py`

Demonstrates performance differences between:
- Top-level column access vs struct field access
- Column chunk organization in Parquet
- Predicate pushdown capabilities

### `optimized_queries.py`

Optimized query patterns for workflow logs:
- Selective column scanning
- Efficient struct field access
- Aggregation patterns

### `workflow_queries.py`

Implementations of P0, P1, P2 query patterns:

| Pattern | Description | Status |
|---------|-------------|--------|
| P0 | Container logs by workflow | Implemented |
| P1 | Live streaming batch | Stub |
| P2 | Download preparation | Stub |

## Running

All scripts can be run from the project root:

```bash
cd mdlh_test
python3 analysis/partition_analysis.py
python3 analysis/performance_demo.py
```

## Key Learnings

1. **Top-level fields are critical** for predicate pushdown
2. **Struct access is slower** due to row reconstruction overhead
3. **Partition column must match query predicate** for pruning
4. **99% pruning achieved** with proper partitioning
