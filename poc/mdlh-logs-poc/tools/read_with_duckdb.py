#!/usr/bin/env python3
"""
Read Iceberg tables using DuckDB's native Iceberg support.

Usage:
    python3 tools/read_with_duckdb.py
"""

import sys
from pathlib import Path

import duckdb

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, fq, NAMESPACE, ENTITY_TABLE


def read_with_duckdb():
    """Read Iceberg table using DuckDB."""
    print("=" * 60)
    print("DuckDB Iceberg Reader")
    print("=" * 60)
    
    # Get table metadata via PyIceberg
    catalog = get_catalog()
    table = catalog.load_table(fq(ENTITY_TABLE))
    
    print(f"\nTable: {fq(ENTITY_TABLE)}")
    print(f"Location: {table.metadata.location}")
    
    # Load into DuckDB via PyArrow
    print("\nLoading table via PyIceberg...")
    arrow_table = table.scan().to_arrow()
    
    print(f"Loaded {len(arrow_table):,} rows")
    
    # Setup DuckDB
    con = duckdb.connect(":memory:")
    con.register("workflow_logs", arrow_table)
    
    # Run some queries
    print("\n--- Sample Queries ---")
    
    queries = [
        ("Row count", "SELECT COUNT(*) as total FROM workflow_logs"),
        ("Log levels", "SELECT level, COUNT(*) as cnt FROM workflow_logs GROUP BY level"),
        ("Recent logs", "SELECT timestamp, level, message FROM workflow_logs ORDER BY timestamp DESC LIMIT 5"),
    ]
    
    for name, sql in queries:
        print(f"\n{name}:")
        result = con.execute(sql).fetchdf()
        print(result.to_string())
    
    print("\n" + "=" * 60)
    print("Done!")


if __name__ == "__main__":
    read_with_duckdb()
