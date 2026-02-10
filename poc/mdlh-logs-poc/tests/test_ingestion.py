#!/usr/bin/env python3
"""
Test ingestion with a single file.

Usage:
    python3 tests/test_ingestion.py
"""

import sys
from pathlib import Path

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, get_s3_client, S3_BUCKET, S3_PREFIX, fq, NAMESPACE


def test_single_file_ingestion():
    """Test ingesting a single Parquet file."""
    print("=" * 60)
    print("Test: Single File Ingestion")
    print("=" * 60)
    
    s3 = get_s3_client()
    catalog = get_catalog()
    
    # Find a sample file
    print(f"\nSearching for files in s3://{S3_BUCKET}/{S3_PREFIX}/...")
    
    paginator = s3.get_paginator("list_objects_v2")
    sample_key = None
    
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX, MaxKeys=50):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                sample_key = obj["Key"]
                break
        if sample_key:
            break
    
    if not sample_key:
        print("No Parquet files found!")
        return False
    
    print(f"Found: {sample_key}")
    
    # Download and read
    import io
    import pyarrow.parquet as pq
    
    print("\nDownloading...")
    response = s3.get_object(Bucket=S3_BUCKET, Key=sample_key)
    body = response["Body"].read()
    
    table = pq.read_table(io.BytesIO(body))
    print(f"Read {len(table)} rows")
    
    # Check schema
    print("\nSchema fields:")
    for field in table.schema:
        print(f"  {field.name}: {field.type}")
    
    # Check for required fields
    required_fields = ['timestamp', 'level', 'message']
    missing = [f for f in required_fields if f not in table.column_names]
    
    if missing:
        print(f"\n❌ Missing required fields: {missing}")
        return False
    
    print("\n✅ All required fields present")
    
    # Check for workflow fields
    workflow_fields = ['atlan_argo_workflow_name', 'atlan_argo_workflow_node']
    for field in workflow_fields:
        if field in table.column_names:
            non_null = len(table) - table[field].null_count
            print(f"  {field}: {non_null}/{len(table)} non-null")
    
    return True


if __name__ == "__main__":
    success = test_single_file_ingestion()
    sys.exit(0 if success else 1)
