#!/usr/bin/env python3
"""
Test schema with a specific file.

Usage:
    python3 tests/test_specific_file.py <s3_key>
"""

import sys
import io
from pathlib import Path

import pyarrow.parquet as pq

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_s3_client, S3_BUCKET


def test_specific_file(s3_key: str):
    """Test reading a specific Parquet file."""
    print("=" * 60)
    print("Test: Specific File")
    print("=" * 60)
    
    s3 = get_s3_client()
    
    print(f"\nFile: s3://{S3_BUCKET}/{s3_key}")
    
    # Download
    print("\nDownloading...")
    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
        body = response["Body"].read()
    except Exception as e:
        print(f"❌ Download failed: {e}")
        return False
    
    print(f"Size: {len(body) / 1024:.1f} KB")
    
    # Read
    print("\nReading...")
    table = pq.read_table(io.BytesIO(body))
    print(f"Rows: {len(table)}")
    
    # Schema
    print("\nSchema:")
    for field in table.schema:
        print(f"  {field.name}: {field.type}")
    
    # Sample data
    print("\nSample (first row):")
    if len(table) > 0:
        df = table.to_pandas().head(1)
        for col in df.columns:
            val = df[col].iloc[0]
            if isinstance(val, str) and len(val) > 50:
                val = val[:50] + "..."
            print(f"  {col}: {val}")
    
    print("\n✅ File read successfully")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 test_specific_file.py <s3_key>")
        print("Example: python3 test_specific_file.py artifacts/apps/mssql/production/observability/logs/year=2025/month=12/day=29/chunk-0-part123.parquet")
        sys.exit(1)
    
    success = test_specific_file(sys.argv[1])
    sys.exit(0 if success else 1)
