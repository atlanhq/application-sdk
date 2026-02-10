# Benchmarks

Performance testing for Iceberg partition strategies and P0 query optimization.

## Summary of Findings

### Partition Strategy Comparison

Tested 3 configurations with the same dataset (~437K rows):

| Configuration | Total Time | Plan | Scan | Sort | Files Scanned |
|--------------|------------|------|------|------|---------------|
| `wf_bucket_month` | **1.278s** | 0.358s | 0.899s | 0.021s | 14 |
| `wf_bucket` | 1.298s | 0.329s | 0.947s | 0.022s | 14 |
| `wf_month` | 1.399s | 0.465s | 0.907s | 0.027s | **9** |

### Winner: `wf_month` (workflow_name + month)

Despite slightly higher total time, `wf_month` is preferred because:

1. **Fewer files scanned (9 vs 14)** - Better partition pruning
2. **Simpler partitioning** - Identity transform, no bucket complexity
3. **Predictable distribution** - Easy to reason about data layout
4. **P0 query optimized** - Direct match on workflow_name

### Key Metrics

- **P0 Query Response:** ~1.3s for 437K rows → 1000 rows returned
- **Partition Pruning:** 99%+ of data skipped via metadata
- **Scan Efficiency:** Only reads files matching workflow_name partition

## Scripts

### `p0_queries.py`

Benchmarks the P0 query (container logs) across all partition configurations.

```bash
# Single workflow test
python3 p0_queries.py --workflow-name "atlan-salesforce-..." --runs 3

# Multi-test benchmark
python3 p0_queries.py --multi --output benchmark_results.json
```

### `scale_test.py`

Tests P0 query performance at scale with real Iceberg scanning.

```bash
python3 scale_test.py
# Tests: 5K, 25K rows (partitioned vs unpartitioned)
```

### `scale_generator.py`

Generates synthetic workflow log data for scale testing.

```python
from scale_generator import WorkflowLogGenerator
generator = WorkflowLogGenerator()
table = generator.generate_batch(num_rows=100000, start_time=datetime.now())
```

### `generate_report.py`

Creates HTML report from benchmark JSON results.

```bash
python3 generate_report.py --results benchmark_results.json
# Outputs: benchmark_results.html
```

## Results Files

- `results/benchmark_results.json` - Single test results
- `results/benchmark_multi_results.json` - Multi-workflow tests
- `results/benchmark_results.html` - Visual comparison report

## Test Queries

### P0: Container Logs Query

```sql
SELECT * 
FROM workflow_logs 
WHERE atlan_argo_workflow_name = 'workflow-name'
  [AND atlan_argo_workflow_node = 'node-name']
ORDER BY timestamp DESC 
LIMIT 1000
```

### Test Workflows Used

1. `atlan-salesforce-1671795233-cron-1766791800` (with/without node filter)
2. `atlan-analytics-cron-1766801400` (with/without node filter)
3. `atlan-redshift-miner-1659011583-cron-1767416400`

## Methodology

1. Load same data into 3 partition configurations
2. Run P0 query 3 times per configuration (average)
3. Measure: plan time, scan time, sort time, files scanned
4. Compare partition pruning effectiveness

## Conclusions

1. **Predicate pushdown is critical** - Filters must use partition columns
2. **Identity partition > Bucket** - For exact-match queries on workflow_name
3. **Month partition prevents explosion** - Bounds partition count over time
4. **Top-level fields required** - Nested struct fields can't use partition pruning
