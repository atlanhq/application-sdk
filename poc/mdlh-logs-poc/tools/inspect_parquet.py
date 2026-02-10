#!/usr/bin/env python3
"""
Inspect Parquet file schema from S3.

Usage:
    python3 tools/inspect_parquet.py
"""

import io
import sys
from pathlib import Path

import pyarrow.parquet as pq

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_s3_client, S3_BUCKET, S3_PREFIX


def inspect_parquet_schema():
    """Inspect schema of a sample Parquet file from S3."""
    print("=" * 60)
    print("Parquet Schema Inspector")
    print("=" * 60)
    
    s3 = get_s3_client()
    
    # Find a sample file
    print(f"\nSearching for Parquet files in s3://{S3_BUCKET}/{S3_PREFIX}/...")
    
    paginator = s3.get_paginator("list_objects_v2")
    sample_key = None
    
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX, MaxKeys=100):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                sample_key = obj["Key"]
                break
        if sample_key:
            break
    
    if not sample_key:
        print("No Parquet files found!")
        return
    
    print(f"\nSample file: {sample_key}")
    
    # Download and inspect
    print("\nDownloading...")
    response = s3.get_object(Bucket=S3_BUCKET, Key=sample_key)
    body = response["Body"].read()
    
    # Read with PyArrow
    table = pq.read_table(io.BytesIO(body))
    
    print(f"\n--- Schema ({len(table.schema)} fields) ---")
    for field in table.schema:
        print(f"  {field.name}: {field.type}")
    
    print(f"\n--- Statistics ---")
    print(f"  Rows: {len(table):,}")
    print(f"  Size: {len(body) / 1024:.1f} KB")
    
    # Check for 'atlan' fields
    print("\n--- Atlan-related fields ---")
    for field in table.schema:
        if 'atlan' in field.name.lower():
            col = table[field.name]
            non_null = len(col) - col.null_count
            print(f"  {field.name}: {non_null:,} non-null values")
    
    # Sample values
    print("\n--- Sample data (first 3 rows) ---")
    df = table.to_pandas().head(3)
    for idx, row in df.iterrows():
        print(f"\nRow {idx}:")
        for col in ['timestamp', 'level', 'message', 'atlan_argo_workflow_name']:
            if col in df.columns:
                val = row[col]
                if isinstance(val, str) and len(val) > 80:
                    val = val[:80] + "..."
                print(f"  {col}: {val}")


if __name__ == "__main__":
    inspect_parquet_schema()
