#!/usr/bin/env python3
"""
Ingest Parquet files into multiple Iceberg table configurations.

For each configuration:
1. Drop table if exists
2. Create table with config's schema + partitioning + sort order
3. Load same Parquet files into this table
4. Track ingestion time

Usage:
    python3 ingestion/ingest_multiple_configs.py --parquet-dir ./data/parquet_files
"""

import sys
import time
from pathlib import Path
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, fq, NAMESPACE
from ingestion.schema import CONFIGURATIONS


def log(msg):
    """Print timestamped log message and flush immediately."""
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] {msg}", flush=True)

def ingest_config(config_name, parquet_files, catalog):
    """Ingest Parquet files into a specific configuration."""
    config = CONFIGURATIONS[config_name]
    table_name = config['table_name']
    full_table_name = fq(table_name)
    
    log(f"")
    log(f"{'='*60}")
    log(f"Configuration: {config_name}")
    log(f"Table: {full_table_name}")
    log(f"Description: {config['description']}")
    log(f"{'='*60}")
    
    # Drop table if exists
    log(f"Checking if table exists...")
    try:
        log(f"Dropping existing table: {full_table_name}...")
        catalog.drop_table(full_table_name)
        log(f"✅ Dropped existing table: {full_table_name}")
    except Exception as e:
        log(f"ℹ️  Table doesn't exist (will create)")
    
    # Create table
    log(f"Creating table with partitioning: {config['partition_config']}")
    log(f"Sort order: {config['sort_order']}")
    
    try:
        log(f"Sending create_table request to Polaris...")
        table = catalog.create_table(
            identifier=full_table_name,
            schema=config['schema'],
            partition_spec=config['partition_spec']
        )
        log(f"✅ Created table: {full_table_name}")
    except Exception as e:
        log(f"❌ Failed to create table: {e}")
        import traceback
        traceback.print_exc()
        return None
    
    # Ingest Parquet files
    log(f"Starting ingestion of {len(parquet_files)} files...")
    start_time = time.time()
    total_rows = 0
    files_processed = 0
    errors = 0
    
    for i, parquet_file in enumerate(parquet_files):
        file_start = time.time()
        file_name = parquet_file.name
        
        try:
            # Read Parquet file
            log(f"[{i+1}/{len(parquet_files)}] Reading {file_name}...")
            source_table = pq.read_table(parquet_file)
            rows_in_file = len(source_table)
            log(f"[{i+1}/{len(parquet_files)}] Read {rows_in_file:,} rows from {file_name}")
            
            # Append to table (this uploads to S3)
            log(f"[{i+1}/{len(parquet_files)}] Uploading to S3... (this may take a while)")
            table.append(source_table)
            
            file_time = time.time() - file_start
            total_rows += rows_in_file
            files_processed += 1
            
            elapsed = time.time() - start_time
            rate = total_rows / elapsed if elapsed > 0 else 0
            eta_remaining = (len(parquet_files) - i - 1) * (elapsed / (i + 1)) if i > 0 else 0
            
            log(f"[{i+1}/{len(parquet_files)}] ✅ Done in {file_time:.1f}s | "
                f"Total: {total_rows:,} rows | "
                f"Rate: {rate:.0f} rows/s | "
                f"ETA: {eta_remaining/60:.1f} min")
        
        except Exception as e:
            errors += 1
            log(f"[{i+1}/{len(parquet_files)}] ⚠️ ERROR: {e}")
            import traceback
            traceback.print_exc()
            continue
    
    ingestion_time = time.time() - start_time
    
    log(f"")
    log(f"{'='*40}")
    log(f"INGESTION COMPLETE for {config_name}")
    log(f"{'='*40}")
    log(f"  Files processed: {files_processed}/{len(parquet_files)}")
    log(f"  Errors: {errors}")
    log(f"  Rows ingested: {total_rows:,}")
    log(f"  Total time: {ingestion_time:.1f}s ({ingestion_time/60:.1f} min)")
    log(f"  Throughput: {total_rows/ingestion_time:.0f} rows/sec")
    
    # Verify row count (optional, can be slow)
    log(f"Verifying row count in table (scanning)...")
    try:
        scan = table.scan()
        final_count = scan.to_arrow().num_rows
        log(f"✅ Verified: {final_count:,} rows in table")
    except Exception as e:
        log(f"⚠️ Could not verify row count: {e}")
    
    return {
        'config_name': config_name,
        'table_name': full_table_name,
        'files_processed': files_processed,
        'rows_ingested': total_rows,
        'ingestion_time': ingestion_time,
        'errors': errors
    }

def ingest_all_configs(parquet_dir, config_filter=None):
    """Ingest Parquet files into all configurations."""
    parquet_path = Path(parquet_dir)
    
    # Find all Parquet files
    parquet_files = sorted(parquet_path.glob("*.parquet"))
    
    if not parquet_files:
        log(f"❌ No Parquet files found in {parquet_dir}")
        return
    
    log(f"Found {len(parquet_files)} Parquet files in {parquet_dir}")
    
    # Calculate total size
    total_size = sum(f.stat().st_size for f in parquet_files)
    log(f"Total data size: {total_size / 1024 / 1024:.1f} MB")
    
    log(f"Connecting to Polaris catalog...")
    catalog = get_catalog()
    log(f"✅ Connected to catalog")
    
    results = []
    
    # Filter configs if specified
    configs_to_run = list(CONFIGURATIONS.keys())
    if config_filter:
        configs_to_run = [c for c in configs_to_run if c in config_filter]
    
    log(f"Will ingest into {len(configs_to_run)} configurations: {configs_to_run}")
    
    # Ingest into each configuration
    for idx, config_name in enumerate(configs_to_run):
        log(f"")
        log(f"#" * 60)
        log(f"# CONFIG {idx+1}/{len(configs_to_run)}: {config_name}")
        log(f"#" * 60)
        
        result = ingest_config(config_name, parquet_files, catalog)
        if result:
            results.append(result)
    
    # Summary
    log(f"")
    log(f"{'='*60}")
    log(f"FINAL INGESTION SUMMARY")
    log(f"{'='*60}")
    
    for result in results:
        log(f"")
        log(f"{result['config_name']}:")
        log(f"  Table: {result['table_name']}")
        log(f"  Rows: {result['rows_ingested']:,}")
        log(f"  Errors: {result.get('errors', 0)}")
        log(f"  Time: {result['ingestion_time']:.1f}s ({result['ingestion_time']/60:.1f} min)")
        if result['ingestion_time'] > 0:
            log(f"  Throughput: {result['rows_ingested']/result['ingestion_time']:.0f} rows/sec")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Ingest Parquet files into multiple Iceberg configurations')
    parser.add_argument('--parquet-dir', required=True, help='Directory containing Parquet files')
    parser.add_argument('--config', help='Run only specific config (comma-separated)', default=None)
    
    args = parser.parse_args()
    
    config_filter = None
    if args.config:
        config_filter = [c.strip() for c in args.config.split(',')]
    
    log(f"Starting ingestion at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Parquet directory: {args.parquet_dir}")
    if config_filter:
        log(f"Config filter: {config_filter}")
    
    ingest_all_configs(args.parquet_dir, config_filter)
