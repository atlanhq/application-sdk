#!/usr/bin/env python3
"""
Quick verification that data was ingested correctly.

Usage:
    python3 tools/quick_verify.py
"""

import sys
from pathlib import Path

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, fq, NAMESPACE, ENTITY_TABLE, CHECKPOINT_TABLE


def verify_ingestion():
    """Verify that data was ingested correctly."""
    print("=" * 60)
    print("Quick Ingestion Verification")
    print("=" * 60)
    
    catalog = get_catalog()
    
    # List tables
    print(f"\nTables in {NAMESPACE}:")
    tables = catalog.list_tables(NAMESPACE)
    for ns, name in tables:
        print(f"  - {ns}.{name}")
    
    # Check entity table
    print(f"\n--- Entity Table ({ENTITY_TABLE}) ---")
    try:
        entity_table = catalog.load_table(fq(ENTITY_TABLE))
        scan = entity_table.scan()
        arrow_table = scan.to_arrow()
        
        print(f"Row count: {len(arrow_table):,}")
        
        # Show schema
        print("\nSchema:")
        for field in arrow_table.schema:
            print(f"  {field.name}: {field.type}")
        
        # Preview
        print("\nFirst 5 rows:")
        df = arrow_table.to_pandas().head(5)
        print(df.to_string())
        
        # Non-null counts
        print("\nNon-null counts (sample):")
        for col in ['level', 'atlan_argo_workflow_name', 'atlan_argo_workflow_node', 'trace_id']:
            if col in arrow_table.column_names:
                non_null = arrow_table[col].null_count
                total = len(arrow_table)
                print(f"  {col}: {total - non_null:,} / {total:,}")
    
    except Exception as e:
        print(f"Error: {e}")
    
    # Check checkpoint table
    print(f"\n--- Checkpoint Table ({CHECKPOINT_TABLE}) ---")
    try:
        checkpoint_table = catalog.load_table(fq(CHECKPOINT_TABLE))
        checkpoint_arrow = checkpoint_table.scan().to_arrow()
        
        print(f"Files checkpointed: {len(checkpoint_arrow):,}")
        
        if len(checkpoint_arrow) > 0:
            print("\nLast 5 checkpointed files:")
            for filename in checkpoint_arrow['filename'].to_pylist()[-5:]:
                print(f"  {filename}")
    
    except Exception as e:
        print(f"Error: {e}")
    
    print("\n" + "=" * 60)
    print("Verification complete!")


if __name__ == "__main__":
    verify_ingestion()
