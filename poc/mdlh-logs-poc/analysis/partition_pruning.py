#!/usr/bin/env python3
"""
Explains Iceberg partition pruning behavior.

Key Findings:
1. Predicate pushdown only works on partition columns
2. No automatic column equivalence detection
3. Query planner can't correlate different columns

Usage:
    python3 analysis/partition_pruning.py
"""

import sys
from pathlib import Path

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, fq
from ingestion.schema import CONFIGURATIONS


def explain_partition_pruning():
    """Explain how Iceberg partition pruning works."""
    
    print("=" * 70)
    print("ICEBERG PARTITION PRUNING EXPLAINED")
    print("=" * 70)
    
    print("""
    
SCENARIO
--------
You have a table partitioned by `atlan_argo_workflow_name`.
You query: WHERE trace_id = 'some-workflow-name'

QUESTION: Does this use partition pruning?

ANSWER: NO! Even if trace_id and workflow_name have the same value.

WHY?
----
Iceberg's partition pruning ONLY works when:
1. The filter column IS a partition column, OR
2. The filter column is DERIVED from a partition column via transform

The query planner has NO WAY to know that:
  trace_id = 'atlan-salesforce-123'
  
...would match the same rows as:
  atlan_argo_workflow_name = 'atlan-salesforce-123'

SOLUTION
--------
Always query on the PARTITION COLUMN directly:

  ❌ WHERE trace_id = 'workflow-name'           -- Full scan
  ✅ WHERE atlan_argo_workflow_name = 'workflow-name'  -- Partition pruning

DEMONSTRATION
-------------
""")
    
    catalog = get_catalog()
    config = CONFIGURATIONS['wf_month_partition']
    table = catalog.load_table(fq(config['table_name']))
    
    # Show partition spec
    print("Partition spec:")
    for field in table.metadata.partition_spec().fields:
        print(f"  Source: {field.source_id} -> {field.name} ({field.transform})")
    
    # Query with partition column
    from pyiceberg.expressions import EqualTo
    
    test_value = "atlan-salesforce-1671795233-cron-1766791800"
    
    print(f"\nTest value: {test_value}")
    
    # Full scan
    all_files = list(table.scan().plan_files())
    print(f"\nFull scan: {len(all_files)} files")
    
    # Filter on partition column
    filtered = list(table.scan(
        row_filter=EqualTo("atlan_argo_workflow_name", test_value)
    ).plan_files())
    print(f"Filter on partition column: {len(filtered)} files")
    
    # Filter on trace_id (NOT partition column)
    trace_filtered = list(table.scan(
        row_filter=EqualTo("trace_id", test_value)
    ).plan_files())
    print(f"Filter on trace_id (non-partition): {len(trace_filtered)} files")
    
    print("""
    
CONCLUSION
----------
- Filtering on partition column: PRUNES data (fewer files)
- Filtering on non-partition column: FULL SCAN (all files)

This is why we extracted `atlan_argo_workflow_name` to the top level
and made it a partition key!
""")


if __name__ == "__main__":
    explain_partition_pruning()
