#!/usr/bin/env python3
"""
Interactive DuckDB Query Interface for Iceberg tables.

Usage:
    python3 tools/duckdb_cli.py

Commands:
    - Type SQL queries and press Enter
    - 'tables' - show available tables
    - 'schema <table>' - show table schema
    - 'samples' - show sample queries
    - 'quit' or 'exit' - exit
"""

import sys
from pathlib import Path

import duckdb

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, fq, ENTITY_TABLE, CHECKPOINT_TABLE, NAMESPACE

SAMPLE_QUERIES = {
    "1": ("First 20 rows", "SELECT * FROM workflow_logs LIMIT 20"),
    "2": ("Count by log level", "SELECT level, COUNT(*) as count FROM workflow_logs GROUP BY level ORDER BY count DESC"),
    "3": ("Recent logs", "SELECT timestamp, level, message FROM workflow_logs ORDER BY timestamp DESC LIMIT 30"),
    "4": ("Error logs", "SELECT * FROM workflow_logs WHERE level = 'ERROR' LIMIT 50"),
    "5": ("Logs with workflow ID", "SELECT * FROM workflow_logs WHERE argo_workflow_run_id IS NOT NULL LIMIT 30"),
    "6": ("Count by logger", "SELECT logger_name, COUNT(*) as count FROM workflow_logs GROUP BY logger_name ORDER BY count DESC LIMIT 15"),
    "7": ("Checkpointed files", "SELECT * FROM checkpoints LIMIT 30"),
    "8": ("Message patterns", "SELECT message, COUNT(*) as cnt FROM workflow_logs GROUP BY message ORDER BY cnt DESC LIMIT 15"),
    "9": ("Time range", "SELECT MIN(timestamp) as min_ts, MAX(timestamp) as max_ts FROM workflow_logs"),
    "10": ("Workflow ID stats", "SELECT COUNT(*) as total, COUNT(argo_workflow_run_id) as with_id FROM workflow_logs"),
}


def setup_duckdb_with_iceberg(con):
    """Setup DuckDB with Iceberg tables via PyIceberg."""
    print("Loading Iceberg tables...")
    
    catalog = get_catalog()
    
    # Load entity table
    try:
        entity_table = catalog.load_table(fq(ENTITY_TABLE))
        entity_arrow = entity_table.scan().to_arrow()
        con.register("workflow_logs", entity_arrow)
        print(f"✓ Loaded workflow_logs ({len(entity_arrow)} rows)")
    except Exception as e:
        print(f"✗ Could not load workflow_logs: {e}")
    
    # Load checkpoint table
    try:
        checkpoint_table = catalog.load_table(fq(CHECKPOINT_TABLE))
        checkpoint_arrow = checkpoint_table.scan().to_arrow()
        con.register("checkpoints", checkpoint_arrow)
        print(f"✓ Loaded checkpoints ({len(checkpoint_arrow)} rows)")
    except Exception as e:
        print(f"✗ Could not load checkpoints: {e}")


def show_samples():
    """Display sample queries."""
    print("\nSample Queries:")
    print("-" * 60)
    for key, (name, query) in SAMPLE_QUERIES.items():
        print(f"  {key}. {name}")
    print("\nType a number to run that query, or enter your own SQL.")


def run_query(con, sql):
    """Execute a query and display results."""
    try:
        result = con.execute(sql).fetchdf()
        print(result.to_string())
        print(f"\n({len(result)} rows)")
    except Exception as e:
        print(f"Error: {e}")


def main():
    print("=" * 60)
    print("DuckDB Iceberg Query Interface")
    print("=" * 60)
    
    con = duckdb.connect(":memory:")
    setup_duckdb_with_iceberg(con)
    
    print("\nCommands: 'tables', 'schema <table>', 'samples', 'quit'")
    print("Or enter SQL queries directly.\n")
    
    while True:
        try:
            query = input("duckdb> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break
        
        if not query:
            continue
        
        lower_query = query.lower()
        
        if lower_query in ('quit', 'exit', 'q'):
            print("Goodbye!")
            break
        
        if lower_query == 'tables':
            print("\nAvailable tables:")
            print("  - workflow_logs")
            print("  - checkpoints")
            continue
        
        if lower_query.startswith('schema '):
            table_name = query.split()[1]
            try:
                result = con.execute(f"DESCRIBE {table_name}").fetchdf()
                print(result.to_string())
            except Exception as e:
                print(f"Error: {e}")
            continue
        
        if lower_query == 'samples':
            show_samples()
            continue
        
        # Check if it's a sample query number
        if query in SAMPLE_QUERIES:
            name, sql = SAMPLE_QUERIES[query]
            print(f"\n{name}:")
            print(f"  {sql}\n")
            run_query(con, sql)
            continue
        
        # Otherwise, treat as SQL
        run_query(con, query)


if __name__ == "__main__":
    main()
