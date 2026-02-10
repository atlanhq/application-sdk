#!/usr/bin/env python3
"""
Analyze partition strategy impact on query performance.

Usage:
    python3 analysis/partition_analysis.py
"""

import sys
import time
from pathlib import Path

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, fq, NAMESPACE
from ingestion.schema import CONFIGURATIONS


def analyze_partition_impact():
    """Analyze how partitioning affects query performance."""
    print("=" * 60)
    print("Partition Strategy Analysis")
    print("=" * 60)
    
    catalog = get_catalog()
    
    for config_name, config in CONFIGURATIONS.items():
        table_name = fq(config['table_name'])
        print(f"\n--- {config_name} ---")
        print(f"Table: {table_name}")
        print(f"Partition: {config['partition_config']}")
        
        try:
            table = catalog.load_table(table_name)
            
            # Get partition info
            print("\nPartition spec:")
            for field in table.metadata.partition_spec().fields:
                print(f"  {field.name}: {field.transform}")
            
            # Count data files
            scan = table.scan()
            files = list(scan.plan_files())
            print(f"\nTotal data files: {len(files)}")
            
            # Sample query to show partition pruning
            from pyiceberg.expressions import EqualTo
            
            test_workflow = "atlan-salesforce-1671795233-cron-1766791800"
            filtered_scan = table.scan(
                row_filter=EqualTo("atlan_argo_workflow_name", test_workflow)
            )
            filtered_files = list(filtered_scan.plan_files())
            
            print(f"Files after filter on workflow_name: {len(filtered_files)}")
            pruning_pct = (1 - len(filtered_files) / len(files)) * 100 if files else 0
            print(f"Partition pruning: {pruning_pct:.1f}% of files skipped")
            
        except Exception as e:
            print(f"Error: {e}")
    
    print("\n" + "=" * 60)
    print("KEY INSIGHT:")
    print("Identity partition on workflow_name enables exact-match pruning.")
    print("Bucket partition scatters data across buckets, reducing pruning efficiency.")


if __name__ == "__main__":
    analyze_partition_impact()
