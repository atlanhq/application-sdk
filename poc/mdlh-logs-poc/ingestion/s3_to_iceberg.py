#!/usr/bin/env python3
"""
Parallel S3 -> Iceberg ingestion POC with pluggable parsers.

Features:
- Pluggable parser system supporting multiple log formats (Parquet, OTLP JSON)
- Delete/recreate tables: argo_workflow_log, workflow_log_checkpoint, workflow_log_entity
- Main table schema uses string for argo_workflow_run_id to avoid parse errors
- Parallel download + normalize (ThreadPoolExecutor)
- Batch appends on main thread to reduce commit conflicts (configurable batch_size)
- Checkpoint table with required filename (non-nullable) appended after successful batch commit
- Per-file timing + summary stats

Usage:
    python3 ingestion/s3_to_iceberg.py
    # or from project root:
    python3 -m ingestion.s3_to_iceberg

Environment variables:
    S3_ENDPOINT - S3 endpoint URL (for MinIO)
    S3_FORCE_PATH_STYLE - Use path-style S3 URLs (for MinIO)
"""

import io
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Tuple, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from pyiceberg.schema import Schema
from pyiceberg.types import (
    NestedField,
    IntegerType,
    StringType,
    DoubleType,
    StructType,
)

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import all config from centralized config file
from config import (
    get_catalog,
    get_s3_client,
    fq,
    S3_BUCKET as BUCKET,
    S3_PREFIX as PREFIX,
    NAMESPACE,
    ENTITY_TABLE,
    CHECKPOINT_TABLE,
    WORKER_THREADS,
    BATCH_SIZE,
    COMMIT_RETRIES,
    COMMIT_RETRY_BASE,
)

# Import pluggable parser system
try:
    # When running as module: python -m ingestion.s3_to_iceberg
    from ingestion.parsers.base import get_default_registry
except ImportError:
    # When running directly: python ingestion/s3_to_iceberg.py
    from parsers.base import get_default_registry

# S3 client from config
s3 = get_s3_client()

# Initialize parser registry
parser_registry = get_default_registry()
print(f"Available parsers: {parser_registry.list_parsers()}")

# Load Polaris catalog from config
catalog = get_catalog()
catalog.create_namespace_if_not_exists(NAMESPACE)

# ---------------------------------------
# Drop existing tables if present (safe)
# ---------------------------------------
def drop_if_exists(identifier: str):
    try:
        # list_tables returns tuples (namespace, name) for membership checks,
        # but drop_table expects identifier string in many catalogs.
        print(f"Attempting to drop table if exists: {identifier}")
        catalog.drop_table(identifier)
        print(f"Dropped table: {identifier}")
    except Exception as e:
        # Either table didn't exist or drop failed - continue
        print(f"Could not drop {identifier} (may not exist): {e}")


# Drop tables to recreate with new schema (application_name added)
# Uncomment to recreate tables with new schema
# for t in [CHECKPOINT_TABLE, ENTITY_TABLE]:
#     drop_if_exists(fq(t))

# ---------------------------------------
# Define new Iceberg schemas
# Updated to match actual parquet schema from S3
# ---------------------------------------
from pyiceberg.types import LongType

main_schema = Schema(
    fields=[
        NestedField(1, "timestamp", DoubleType(), required=False),
        NestedField(2, "level", StringType(), required=False),
        NestedField(3, "logger_name", StringType(), required=False),
        NestedField(4, "message", StringType(), required=False),
        NestedField(5, "file", StringType(), required=False),
        NestedField(6, "line", LongType(), required=False),  # int64 in parquet
        NestedField(7, "function", StringType(), required=False),
        # These are null in parquet but keeping as strings for future compatibility
        NestedField(8, "argo_workflow_run_id", StringType(), required=False),
        NestedField(9, "argo_workflow_run_uuid", StringType(), required=False),
        # Top-level fields extracted from extra struct for easier querying
        NestedField(10, "trace_id", StringType(), required=False),  # Extracted from extra.trace_id
        NestedField(11, "atlan_argo_workflow_name", StringType(), required=False),  # Extracted from extra.atlan-argo-workflow-name
        NestedField(12, "atlan_argo_workflow_node", StringType(), required=False),  # Extracted from extra.atlan-argo-workflow-node
        NestedField(14, "application_name", StringType(), required=False),  # Extracted from resource attributes
        # Changed from 'payload' to 'extra' to match parquet field name
        NestedField(
            13,
            "extra",
            StructType(
                NestedField(11, "activity_id", StringType(), required=False),  # was 'id' in old schema
                NestedField(12, "activity_type", StringType(), required=False),  # null in parquet
                NestedField(13, "atlan-argo-workflow-name", StringType(), required=False),  # string in some files
                NestedField(14, "atlan-argo-workflow-node", StringType(), required=False),  # string in some files
                NestedField(15, "attempt", StringType(), required=False),  # null in parquet
                NestedField(16, "client_host", StringType(), required=False),  # null in parquet
                NestedField(17, "duration_ms", StringType(), required=False),  # null in parquet
                NestedField(18, "heartbeat_timeout", StringType(), required=False),  # null in parquet
                NestedField(19, "log_type", StringType(), required=False),  # null in parquet
                NestedField(20, "method", StringType(), required=False),  # null in parquet
                NestedField(21, "namespace", StringType(), required=False),  # null in parquet
                NestedField(22, "path", StringType(), required=False),  # null in parquet
                NestedField(23, "request_id", StringType(), required=False),
                NestedField(24, "run_id", StringType(), required=False),  # null in parquet
                NestedField(25, "schedule_to_close_timeout", StringType(), required=False),  # null in parquet
                NestedField(26, "schedule_to_start_timeout", StringType(), required=False),  # null in parquet
                NestedField(27, "start_to_close_timeout", StringType(), required=False),  # null in parquet
                NestedField(28, "status_code", StringType(), required=False),  # null in parquet
                NestedField(29, "task_queue", StringType(), required=False),  # null in parquet
                NestedField(30, "trace_id", StringType(), required=False),  # null in parquet (new field)
                NestedField(31, "url", StringType(), required=False),  # null in parquet
                NestedField(32, "workflow_id", StringType(), required=False),  # null in parquet
                NestedField(33, "workflow_type", StringType(), required=False),  # null in parquet
            ),
            required=False,
        ),
        NestedField(33, "month", LongType(), required=False),  # int64 in parquet
    ]
)


# Create entity table (empty identical schema) if desired
entity_identifier = fq(ENTITY_TABLE)
if (NAMESPACE, ENTITY_TABLE) not in catalog.list_tables(NAMESPACE):
    print(f"Creating entity table: {entity_identifier}")
    entity_table = catalog.create_table(identifier=entity_identifier, schema=main_schema)
else:
    entity_table = catalog.load_table(entity_identifier)

# Checkpoint schema (use dict style for pyiceberg Schema constructor)
checkpoint_schema = Schema(
    fields=[
        {"id": 1000, "name": "filename", "type": "string", "required": True}
    ],
    identifier_field_names=["filename"],
)

checkpoint_identifier = fq(CHECKPOINT_TABLE)
if (NAMESPACE, CHECKPOINT_TABLE) not in catalog.list_tables(NAMESPACE):
    print(f"Creating checkpoint table: {checkpoint_identifier}")
    checkpoint_table = catalog.create_table(identifier=checkpoint_identifier, schema=checkpoint_schema)
else:
    checkpoint_table = catalog.load_table(checkpoint_identifier)

# ---------------------------------------
# PyArrow target schema (must match Iceberg main_schema exactly)
# Updated to match parquet schema: extra instead of payload, int64 for line/month
# ---------------------------------------
arrow_schema = pa.schema([
    pa.field("timestamp", pa.float64()),
    pa.field("level", pa.string()),
    pa.field("logger_name", pa.string()),
    pa.field("message", pa.string()),
    pa.field("file", pa.string()),
    pa.field("line", pa.int64()),  # Changed to int64 to match parquet
    pa.field("function", pa.string()),
    pa.field("argo_workflow_run_id", pa.string()),
    pa.field("argo_workflow_run_uuid", pa.string()),
    # Top-level fields extracted from extra struct for easier querying
    pa.field("trace_id", pa.string()),  # Extracted from extra.trace_id
    pa.field("atlan_argo_workflow_name", pa.string()),  # Extracted from extra.atlan-argo-workflow-name
    pa.field("atlan_argo_workflow_node", pa.string()),  # Extracted from extra.atlan-argo-workflow-node
    pa.field("application_name", pa.string()),  # Extracted from resource attributes
    pa.field(
        "extra",  # Changed from 'payload' to match parquet
        pa.struct([
            pa.field("activity_id", pa.string()),  # Changed from 'id' to match parquet
            pa.field("activity_type", pa.string()),  # Changed to string since null in parquet
            pa.field("atlan-argo-workflow-name", pa.string()),  # string in some files
            pa.field("atlan-argo-workflow-node", pa.string()),  # string in some files
            pa.field("attempt", pa.string()),  # Changed to string since null in parquet
            pa.field("client_host", pa.string()),  # Changed to string since null in parquet
            pa.field("duration_ms", pa.string()),  # Changed to string since null in parquet
            pa.field("heartbeat_timeout", pa.string()),  # Changed to string since null in parquet
            pa.field("log_type", pa.string()),  # Changed to string since null in parquet
            pa.field("method", pa.string()),  # Changed to string since null in parquet
            pa.field("namespace", pa.string()),  # Changed to string since null in parquet
            pa.field("path", pa.string()),  # Changed to string since null in parquet
            pa.field("request_id", pa.string()),
            pa.field("run_id", pa.string()),  # Changed to string since null in parquet
            pa.field("schedule_to_close_timeout", pa.string()),  # Changed to string since null in parquet
            pa.field("schedule_to_start_timeout", pa.string()),  # Changed to string since null in parquet
            pa.field("start_to_close_timeout", pa.string()),  # Changed to string since null in parquet
            pa.field("status_code", pa.string()),  # Changed to string since null in parquet
            pa.field("task_queue", pa.string()),  # Changed to string since null in parquet
            pa.field("trace_id", pa.string()),  # Changed to string since null in parquet (new field)
            pa.field("url", pa.string()),  # Changed to string since null in parquet
            pa.field("workflow_id", pa.string()),  # Changed to string since null in parquet
            pa.field("workflow_type", pa.string()),  # Changed to string since null in parquet
        ])
    ),
    pa.field("month", pa.int64()),  # Changed to int64 to match parquet
])

# ---------------------------------------
# Helper: load checkpoint set
# ---------------------------------------
def load_checkpoint_set() -> set:
    try:
        arr = checkpoint_table.scan().to_arrow()
        if arr is None or len(arr) == 0:
            return set()
        # ensure column exists
        if "filename" in arr.schema.names:
            return set(arr["filename"].to_pylist())
        return set()
    except Exception as e:
        print("Could not read checkpoint table:", e)
        return set()

already_processed = load_checkpoint_set()
print(f"Checkpoint loaded: {len(already_processed)} files already processed")

# ---------------------------------------
# S3 listing function (use zero-padded month/day)
# Supports both Parquet and JSON files via pluggable parsers
# ---------------------------------------
def list_s3_files(year: int, month: int, day: int, hour: int = None) -> List[str]:
    """
    List S3 files for a given date partition.

    Args:
        year: Year number
        month: Month number (1-12)
        day: Day number (1-31)
        hour: Optional hour number (0-23) for hourly partitions

    Returns:
        List of S3 keys for files that can be parsed
    """
    day_s = f"{day:02d}"
    month_s = f"{month:02d}"

    if hour is not None:
        hour_s = f"{hour:02d}"
        prefix = f"{PREFIX}/year={year}/month={month_s}/day={day_s}/hour={hour_s}/"
    else:
        prefix = f"{PREFIX}/year={year}/month={month_s}/day={day_s}/"

    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            # Check if any parser can handle this file
            if parser_registry.get_parser(k) is not None:
                keys.append(k)
    return keys


def list_s3_files_by_prefix(prefix: str) -> List[str]:
    """
    List all S3 files under a given prefix.

    Args:
        prefix: S3 prefix to scan

    Returns:
        List of S3 keys for files that can be parsed
    """
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            # Check if any parser can handle this file
            if parser_registry.get_parser(k) is not None:
                keys.append(k)
    return keys

# ---------------------------------------
# Normalize: drop extra columns, add missing with nulls, and ensure types
# ---------------------------------------
def normalize_table(pa_tbl: pa.Table, target_schema: pa.Schema) -> pa.Table:
    """
    Keep only fields in target_schema, add missing fields as nulls and
    cast where possible. For nested struct 'extra', handle carefully.
    Also extract top-level fields from the extra struct.
    """
    # If incoming table has struct fields encoded differently, this may need extension.
    columns = []

    # First, get the extra struct column if it exists
    extra_col = None
    if 'extra' in pa_tbl.column_names:
        extra_col = pa_tbl['extra']

    for field in target_schema:
        name = field.name

        # Special handling for fields that should be extracted from the extra struct
        if name in ['trace_id', 'atlan_argo_workflow_name', 'atlan_argo_workflow_node'] and extra_col is not None:
            # Extract the field from the extra struct
            try:
                # Map schema field names to struct field names
                field_mapping = {
                    'trace_id': 'trace_id',
                    'atlan_argo_workflow_name': 'atlan-argo-workflow-name',
                    'atlan_argo_workflow_name': 'atlan-argo-workflow-node'
                }
                struct_field_name = field_mapping[name]

                extracted_values = []
                for row in extra_col:
                    if row.is_valid:
                        try:
                            value = row[struct_field_name]
                            extracted_values.append(value.as_py() if value.is_valid else None)
                        except (KeyError, IndexError):
                            extracted_values.append(None)
                    else:
                        extracted_values.append(None)
                columns.append(pa.array(extracted_values, type=field.type))
            except Exception as e:
                print(f"Warning: Could not extract {name} from extra struct: {e}")
                columns.append(pa.array([None] * pa_tbl.num_rows, type=field.type))
        elif name in pa_tbl.column_names:
            col = pa_tbl[name]
            # Try cast to expected type when possible; otherwise fill nulls
            try:
                if col.type != field.type:
                    col = col.cast(field.type)
            except Exception:
                # if can't cast (e.g., string -> struct), fallback to nulls
                col = pa.array([None] * pa_tbl.num_rows, type=field.type)
            columns.append(col)
        else:
            columns.append(pa.array([None] * pa_tbl.num_rows, type=field.type))
    return pa.Table.from_arrays(columns, schema=target_schema)

# ---------------------------------------
# Worker: download & parse a single file (runs in threadpool)
# Uses pluggable parser system for format detection
# Returns (key, pa.Table, bytes_len, timings dict) or (key, Exception)
# ---------------------------------------
def worker_download_normalize(key: str) -> Tuple[str, Optional[pa.Table], Optional[int], dict, Optional[Exception]]:
    timings = {}
    t0 = time.time()
    try:
        # Find appropriate parser
        parser = parser_registry.get_parser(key)
        if parser is None:
            raise ValueError(f"No parser found for file: {key}")

        s3_start = time.time()
        resp = s3.get_object(Bucket=BUCKET, Key=key)
        body = resp["Body"].read()
        s3_end = time.time()
        timings['s3_download'] = s3_end - s3_start
        bytes_len = len(body)

        parse_start = time.time()
        # Use pluggable parser instead of hardcoded parquet
        norm = parser.parse(body, arrow_schema)
        parse_end = time.time()
        timings['parse'] = parse_end - parse_start
        timings['parser'] = parser.name

        timings['total_worker'] = time.time() - t0
        return key, norm, bytes_len, timings, None
    except Exception as e:
        timings['total_worker'] = time.time() - t0
        return key, None, None, timings, e

# ---------------------------------------
# Append batch with retries
# ---------------------------------------
def commit_batch(tables: List[pa.Table], keys_in_batch: List[str]) -> bool:
    """Concatenate tables and append to Iceberg with retries.
       On success, also append checkpoint rows.
    """
    concat_tbl = pa.concat_tables(tables, promote=True, use_threads=False)
    rows_in_batch = concat_tbl.num_rows
    if rows_in_batch == 0:
        # nothing to commit
        return True

    # Try append with retries
    attempt = 0
    while attempt < COMMIT_RETRIES:
        try:
            # append to main table
            entity_table.append(concat_tbl)
            # append checkpoint entries (non-nullable filename)
            checkpoint_arrow_schema = pa.schema([pa.field("filename", pa.string(), nullable=False)])
            checkpoint_rows = [{"filename": k} for k in keys_in_batch]
            cp_tbl = pa.Table.from_pylist(checkpoint_rows, schema=checkpoint_arrow_schema)
            checkpoint_table.append(cp_tbl)
            return True
        except Exception as ex:
            attempt += 1
            wait = COMMIT_RETRY_BASE * (2 ** (attempt - 1))
            print(f"Commit attempt {attempt} failed for batch (rows={rows_in_batch}). Retrying in {wait:.1f}s. Error: {ex}")
            traceback.print_exc()
            time.sleep(wait)
            # continue and retry
    # after retries
    print("Commit failed after retries for batch. Keys:", keys_in_batch[:10], "...")
    return False

# ---------------------------------------
# Main ingestion: parallel read/normalize, batch sequential commits
# ---------------------------------------
def ingest_partition(year: int, month: int, day: int):
    keys = list_s3_files(year, month, day)
    print(f"Scanning: {PREFIX}/year={year}/month={month:02d}/day={day:02d}/")
    print(f"Total files found: {len(keys)}")
    keys_to_process = [k for k in keys if k not in already_processed]
    print(f"New files: {len(keys_to_process)}")

    if not keys_to_process:
        return

    # Stats
    processed_keys = []
    total_rows = 0
    total_bytes = 0
    start_all = time.time()

    # Use threadpool to download & normalize
    results = []  # list of (key, pa.Table, bytes_len, timings) for successes
    errors = {}   # key -> exception

    with ThreadPoolExecutor(max_workers=WORKER_THREADS) as ex:
        futures = {ex.submit(worker_download_normalize, k): k for k in keys_to_process}
        for fut in as_completed(futures):
            k = futures[fut]
            try:
                key, tbl, bytes_len, timing, err = fut.result()
                if err is not None:
                    errors[k] = err
                    print(f"ERROR worker {k}: {err}")
                    continue
                if tbl is None:
                    errors[k] = Exception("worker returned no table")
                    continue
                results.append((key, tbl, bytes_len, timing))
                print(f"Worker ready: {k} rows={tbl.num_rows} bytes={bytes_len} parser={timing.get('parser')} s3={timing.get('s3_download'):.3f}s parse={timing.get('parse'):.3f}s")
            except Exception as e:
                errors[k] = e
                print(f"Worker exception for {k}: {e}")
                traceback.print_exc()

    # Now commit in batches sequentially to avoid optimistic conflict
    # Order results by their original key order to be deterministic
    key_index = {k: i for i, k in enumerate(keys_to_process)}
    results.sort(key=lambda r: key_index.get(r[0], 0))

    batch_tables = []
    batch_keys = []
    for key, tbl, bytes_len, timing in results:
        batch_tables.append(tbl)
        batch_keys.append(key)

        # If batch size reached, commit
        if len(batch_keys) >= BATCH_SIZE:
            print(f"\nCommitting batch of {len(batch_keys)} files ...")
            ok = commit_batch(batch_tables, batch_keys)
            if not ok:
                print("Batch commit failed — aborting ingestion")
                break
            # update stats
            processed_keys.extend(batch_keys)
            rows = sum(t.num_rows for t in batch_tables)
            total_rows += rows
            total_bytes += sum(x[2] for x in results if x[0] in batch_keys)
            # reset batch
            batch_tables = []
            batch_keys = []

    # Commit remaining
    if batch_keys and commit_batch(batch_tables, batch_keys):
        processed_keys.extend(batch_keys)
        total_rows += sum(t.num_rows for t in batch_tables)
        total_bytes += sum(x[2] for x in results if x[0] in batch_keys)

    elapsed = time.time() - start_all
    print("\n=== PARTITION SUMMARY ===")
    print(f"Files processed: {len(processed_keys)} / {len(keys_to_process)}")
    print(f"Total rows appended: {total_rows}")
    print(f"Total bytes read: {total_bytes}")
    print(f"Elapsed: {elapsed:.3f} sec")
    throughput = total_rows / elapsed if elapsed > 0 else 0.0
    print(f"Throughput: {throughput:.2f} rows/sec")
    if errors:
        print(f"Errors for {len(errors)} files. Sample:")
        for k, e in list(errors.items())[:10]:
            print(f" - {k}: {e}")

# ---------------------------------------
# Ingest all new files (auto-discovery mode)
# ---------------------------------------
def ingest_all_new():
    """
    Discover and ingest all new files from S3.
    Uses checkpoint to skip already-processed files.
    No date parameters needed - scans entire prefix.
    """
    print(f"Scanning S3 bucket: {BUCKET}/{PREFIX}")

    # List all files
    all_keys = list_s3_files_by_prefix(PREFIX)
    print(f"Total files found: {len(all_keys)}")

    # Filter out already-processed files
    keys_to_process = [k for k in all_keys if k not in already_processed]
    print(f"New files to process: {len(keys_to_process)}")

    if not keys_to_process:
        print("No new files to ingest.")
        return

    # Sort by path (which includes timestamp partitions)
    keys_to_process.sort()

    # Stats
    processed_keys = []
    total_rows = 0
    total_bytes = 0
    start_all = time.time()

    # Use threadpool to download & normalize
    results = []  # list of (key, pa.Table, bytes_len, timings) for successes
    errors = {}   # key -> exception

    with ThreadPoolExecutor(max_workers=WORKER_THREADS) as ex:
        futures = {ex.submit(worker_download_normalize, k): k for k in keys_to_process}
        for fut in as_completed(futures):
            k = futures[fut]
            try:
                key, tbl, bytes_len, timing, err = fut.result()
                if err is not None:
                    errors[k] = err
                    print(f"ERROR worker {k}: {err}")
                    continue
                if tbl is None:
                    errors[k] = Exception("worker returned no table")
                    continue
                results.append((key, tbl, bytes_len, timing))
                print(f"Worker ready: {k} rows={tbl.num_rows} bytes={bytes_len} parser={timing.get('parser')} s3={timing.get('s3_download'):.3f}s parse={timing.get('parse'):.3f}s")
            except Exception as e:
                errors[k] = e
                print(f"Worker exception for {k}: {e}")
                traceback.print_exc()

    # Commit in batches sequentially
    key_index = {k: i for i, k in enumerate(keys_to_process)}
    results.sort(key=lambda r: key_index.get(r[0], 0))

    batch_tables = []
    batch_keys = []
    for key, tbl, bytes_len, timing in results:
        batch_tables.append(tbl)
        batch_keys.append(key)

        if len(batch_keys) >= BATCH_SIZE:
            print(f"\nCommitting batch of {len(batch_keys)} files ...")
            ok = commit_batch(batch_tables, batch_keys)
            if not ok:
                print("Batch commit failed — aborting ingestion")
                break
            processed_keys.extend(batch_keys)
            total_rows += sum(t.num_rows for t in batch_tables)
            total_bytes += sum(x[2] for x in results if x[0] in batch_keys)
            batch_tables = []
            batch_keys = []

    # Commit remaining
    if batch_keys and commit_batch(batch_tables, batch_keys):
        processed_keys.extend(batch_keys)
        total_rows += sum(t.num_rows for t in batch_tables)
        total_bytes += sum(x[2] for x in results if x[0] in batch_keys)

    elapsed = time.time() - start_all
    throughput = total_rows / elapsed if elapsed > 0 else 0

    print(f"\n=== Ingestion Summary ===")
    print(f"Files processed: {len(processed_keys)} / {len(keys_to_process)}")
    print(f"Files with errors: {len(errors)}")
    print(f"Total rows: {total_rows}")
    print(f"Total bytes: {total_bytes}")
    print(f"Elapsed: {elapsed:.2f}s")
    print(f"Throughput: {throughput:.2f} rows/sec")

    if errors:
        print(f"\nErrors for {len(errors)} files:")
        for k, e in list(errors.items())[:10]:
            print(f"  - {k}: {e}")


# ---------------------------------------
# Run
# ---------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="S3 to Iceberg ingestion")
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Continuously poll for new files",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Polling interval in seconds (default: 30)",
    )
    args = parser.parse_args()

    if args.poll:
        print(f"Starting continuous ingestion (polling every {args.interval}s)...")
        print("Press Ctrl+C to stop.\n")
        try:
            while True:
                ingest_all_new()
                print(f"\nSleeping {args.interval}s before next poll...\n")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nIngestion stopped by user.")
    else:
        ingest_all_new()
