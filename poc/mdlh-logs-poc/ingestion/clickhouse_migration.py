#!/usr/bin/env python3
"""
Convert ClickHouse CSV export to Parquet files matching Iceberg schema.

Maps ClickHouse columns to target schema:
- Timestamp → timestamp (Unix float64)
- SeverityText → level (WARN→WARNING, DEBUG→DEBUG, INFO→INFO)
- TenantName → logger_name
- Body → message
- WorkflowName → atlan_argo_workflow_name (top-level) + trace_id (top-level)
- WorkflowNode → atlan_argo_workflow_node (top-level)
- Extract month from Timestamp
- Build extra struct with available fields

Usage:
    python3 ingestion/clickhouse_migration.py --input logs.csv --output-dir ./data/parquet_files
"""

import csv
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# Increase CSV field size limit for large log messages
csv.field_size_limit(sys.maxsize)

def map_severity(severity_text):
    """Map ClickHouse SeverityText to standard log levels."""
    mapping = {
        'WARN': 'WARNING',
        'DEBUG': 'DEBUG',
        'INFO': 'INFO',
        'ERROR': 'ERROR',
        'WARNING': 'WARNING'
    }
    return mapping.get(severity_text.upper(), 'INFO')

def build_extra_struct(row):
    """Build extra struct from available ClickHouse data."""
    return {
        'atlan-argo-workflow-name': row.get('WorkflowName'),
        'atlan-argo-workflow-node': row.get('WorkflowNode'),
        'activity_id': None,
        'activity_type': None,
        'attempt': None,
        'client_host': None,
        'duration_ms': None,
        'heartbeat_timeout': None,
        'log_type': None,
        'method': None,
        'namespace': None,
        'path': None,
        'request_id': None,
        'run_id': None,
        'schedule_to_close_timeout': None,
        'schedule_to_start_timeout': None,
        'start_to_close_timeout': None,
        'status_code': None,
        'task_queue': None,
        'trace_id': row.get('WorkflowName'),  # Set to WorkflowName
        'url': None,
        'workflow_id': None,
        'workflow_type': None
    }

def convert_csv_to_parquet(csv_path, output_dir, chunk_size=10000):
    """
    Convert ClickHouse CSV to Parquet files.
    
    Args:
        csv_path: Path to input CSV file
        output_dir: Directory to write Parquet files
        chunk_size: Number of rows per Parquet file
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Define PyArrow schema matching Iceberg schema
    schema = pa.schema([
        pa.field("timestamp", pa.float64()),
        pa.field("level", pa.string()),
        pa.field("logger_name", pa.string()),
        pa.field("message", pa.string()),
        pa.field("file", pa.string()),
        pa.field("line", pa.int64()),
        pa.field("function", pa.string()),
        # REMOVED: argo_workflow_run_id, argo_workflow_run_uuid
        pa.field("trace_id", pa.string()),  # Set to WorkflowName
        pa.field("atlan_argo_workflow_name", pa.string()),
        pa.field("atlan_argo_workflow_node", pa.string()),
        pa.field("extra", pa.struct([
            pa.field("activity_id", pa.string()),
            pa.field("activity_type", pa.string()),
            pa.field("atlan-argo-workflow-name", pa.string()),
            pa.field("atlan-argo-workflow-node", pa.string()),
            pa.field("attempt", pa.string()),
            pa.field("client_host", pa.string()),
            pa.field("duration_ms", pa.string()),
            pa.field("heartbeat_timeout", pa.string()),
            pa.field("log_type", pa.string()),
            pa.field("method", pa.string()),
            pa.field("namespace", pa.string()),
            pa.field("path", pa.string()),
            pa.field("request_id", pa.string()),
            pa.field("run_id", pa.string()),
            pa.field("schedule_to_close_timeout", pa.string()),
            pa.field("schedule_to_start_timeout", pa.string()),
            pa.field("start_to_close_timeout", pa.string()),
            pa.field("status_code", pa.string()),
            pa.field("task_queue", pa.string()),
            pa.field("trace_id", pa.string()),
            pa.field("url", pa.string()),
            pa.field("workflow_id", pa.string()),
            pa.field("workflow_type", pa.string())
        ])),
        pa.field("month", pa.int64())  # Month number (1-12)
    ])
    
    print(f"Reading CSV: {csv_path}")
    print(f"Output directory: {output_dir}")
    print(f"Chunk size: {chunk_size} rows per file")
    
    rows = []
    file_count = 0
    total_rows = 0
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            # Parse timestamp
            try:
                dt = datetime.fromisoformat(row['Timestamp'].replace(' ', 'T'))
                timestamp = dt.timestamp()
                month = dt.month
            except Exception as e:
                print(f"Warning: Could not parse timestamp '{row['Timestamp']}': {e}")
                timestamp = time.time()
                month = 1
            
            # Convert row
            converted_row = {
                'timestamp': timestamp,
                'level': map_severity(row.get('SeverityText', 'INFO')),
                'logger_name': row.get('TenantName', ''),
                'message': row.get('Body', ''),
                'file': None,
                'line': None,
                'function': None,
                'trace_id': row.get('WorkflowName'),  # Set to WorkflowName
                'atlan_argo_workflow_name': row.get('WorkflowName', ''),
                'atlan_argo_workflow_node': row.get('WorkflowNode', ''),
                'extra': build_extra_struct(row),
                'month': month
            }
            
            rows.append(converted_row)
            
            # Write chunk when full
            if len(rows) >= chunk_size:
                write_chunk(rows, schema, output_path, file_count)
                total_rows += len(rows)
                file_count += 1
                rows = []
                print(f"  Written chunk {file_count}: {total_rows} rows total")
        
        # Write remaining rows
        if rows:
            write_chunk(rows, schema, output_path, file_count)
            total_rows += len(rows)
            file_count += 1
    
    print(f"\n✅ Conversion complete!")
    print(f"   Total rows: {total_rows:,}")
    print(f"   Parquet files: {file_count}")
    print(f"   Output directory: {output_dir}")

def write_chunk(rows, schema, output_path, chunk_num):
    """Write a chunk of rows to a Parquet file."""
    table = pa.Table.from_pylist(rows, schema=schema)
    
    # Generate filename: chunk-{n}-part{timestamp}.parquet
    timestamp = int(time.time() * 1000)
    filename = f"chunk-{chunk_num}-part{timestamp}.parquet"
    filepath = output_path / filename
    
    pq.write_table(table, filepath, compression='snappy')
    return filepath

def main():
    parser = argparse.ArgumentParser(description='Convert ClickHouse CSV to Parquet')
    parser.add_argument('--input', required=True, help='Input CSV file path')
    parser.add_argument('--output-dir', required=True, help='Output directory for Parquet files')
    parser.add_argument('--chunk-size', type=int, default=10000, help='Rows per Parquet file (default: 10000)')
    
    args = parser.parse_args()
    
    convert_csv_to_parquet(args.input, args.output_dir, args.chunk_size)

if __name__ == "__main__":
    main()
