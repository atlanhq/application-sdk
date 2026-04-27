# Performance Rules

Comprehensive rules for detecting performance issues in the application-sdk codebase. Each rule includes a unique ID, severity, bad/good code patterns, and expected performance impact.

---

## Blocking in Async

### PERF-001: Synchronous HTTP in Async Code

**Severity:** Critical

**Bad Pattern:**
```python
import requests

async def fetch_asset_details(self, asset_id: str) -> dict:
    # Blocks the entire event loop for the duration of the HTTP call
    response = requests.get(
        f"{self.base_url}/assets/{asset_id}",
        headers=self.headers,
    )
    return response.json()
```

**Good Pattern:**
```python
import httpx

async def fetch_asset_details(self, asset_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{self.base_url}/assets/{asset_id}",
            headers=self.headers,
            timeout=30.0,
        )
        return response.json()
```

**Expected Performance Impact:** A single `requests.get()` blocking for 2 seconds stalls ALL concurrent activities on the Temporal worker. With 10 concurrent activities, effective throughput drops to 1/10th. Per ADR-0010, all I/O in async contexts must be non-blocking.

---

### PERF-002: Synchronous File I/O for Large Files in Async Code

**Severity:** Important

**Bad Pattern:**
```python
async def read_extraction_output(self, path: str) -> list[dict]:
    # Blocks event loop while reading potentially multi-GB file
    with open(path, "r") as f:
        data = json.load(f)
    return data
```

**Good Pattern:**
```python
from application_sdk.execution.heartbeat import run_in_thread
import orjson

async def read_extraction_output(self, path: str) -> list[dict]:
    def _read():
        with open(path, "rb") as f:
            return orjson.loads(f.read())
    return await run_in_thread(_read)
```

**Expected Performance Impact:** Reading a 500MB JSON file synchronously blocks the event loop for 5-15 seconds. Using `run_in_thread()` moves the blocking work to a thread pool, keeping the event loop responsive.

**Important:** Never use `run_in_thread()` to wrap `AtlanClient` calls — the Atlan client is async-only. Use its native async API directly.

---

### PERF-003: `time.sleep()` in Async Code

**Severity:** Critical

**Bad Pattern:**
```python
import time

async def poll_job_status(self, job_id: str) -> str:
    while True:
        status = await self.check_status(job_id)
        if status in ("completed", "failed"):
            return status
        time.sleep(5)  # Blocks entire event loop for 5 seconds
```

**Good Pattern:**
```python
import asyncio

async def poll_job_status(self, job_id: str) -> str:
    while True:
        status = await self.check_status(job_id)
        if status in ("completed", "failed"):
            return status
        await asyncio.sleep(5)  # Yields control back to event loop
```

**Expected Performance Impact:** `time.sleep(5)` freezes all coroutines for 5 seconds per poll iteration. With 30-second polling over a 10-minute job, that is 60 seconds of total event loop freeze per job.

---

## Missing Connection Pooling

### PERF-004: New HTTP/DB Connection Per Request

**Severity:** Critical

**Bad Pattern:**
```python
async def fetch_table_metadata(self, table_name: str) -> dict:
    # Creates a new TCP connection, TLS handshake, etc. for EVERY call
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{self.base_url}/tables/{table_name}")
        return response.json()

async def extract_all(self, tables: list[str]) -> list[dict]:
    results = []
    for table in tables:
        metadata = await self.fetch_table_metadata(table)  # New connection each time
        results.append(metadata)
    return results
```

**Good Pattern:**
```python
class MetadataExtractor:
    def __init__(self, base_url: str):
        self.client = httpx.AsyncClient(
            base_url=base_url,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            timeout=httpx.Timeout(30.0),
        )

    async def fetch_table_metadata(self, table_name: str) -> dict:
        response = await self.client.get(f"/tables/{table_name}")
        return response.json()

    async def extract_all(self, tables: list[str]) -> list[dict]:
        results = []
        for table in tables:
            metadata = await self.fetch_table_metadata(table)
            results.append(metadata)
        return results

    async def close(self):
        await self.client.aclose()
```

**Expected Performance Impact:** TCP + TLS handshake costs 50-200ms per connection. For 1000 tables, that is 50-200 seconds of pure connection overhead eliminated by reusing a pooled client.

---

## Unbounded Memory

### PERF-005: Loading Entire Dataset Into Memory

**Severity:** Critical

**Bad Pattern:**
```python
async def process_extraction(self, file_path: str) -> int:
    with open(file_path, "r") as f:
        all_records = json.load(f)  # Loads entire file into memory
    count = 0
    for record in all_records:
        await self.transform_and_publish(record)
        count += 1
    return count
```

**Good Pattern:**
```python
import ijson

async def process_extraction(self, file_path: str) -> int:
    count = 0
    with open(file_path, "rb") as f:
        for record in ijson.items(f, "item"):
            await self.transform_and_publish(record)
            count += 1
    return count
```

**Expected Performance Impact:** A 2GB JSON file loaded fully into memory requires 4-6GB of RAM (Python object overhead). Streaming with `ijson` keeps memory at O(record_size) regardless of file size. Prevents OOMKills in containerized workers.

---

### PERF-006: Unbounded List Append in Loop

**Severity:** Important

**Bad Pattern:**
```python
async def collect_all_assets(self) -> list[Asset]:
    all_assets = []
    page_token = None
    while True:
        page = await self.api_client.list_assets(page_token=page_token, page_size=500)
        all_assets.extend(page.items)  # Unbounded growth
        if not page.next_token:
            break
        page_token = page.next_token
    return all_assets  # Could be millions of items
```

**Good Pattern:**
```python
from collections.abc import AsyncIterator

async def iter_all_assets(self) -> AsyncIterator[Asset]:
    page_token = None
    while True:
        page = await self.api_client.list_assets(page_token=page_token, page_size=500)
        for item in page.items:
            yield item
        if not page.next_token:
            break
        page_token = page.next_token
```

**Expected Performance Impact:** For a catalog with 500,000 assets, the bad pattern allocates ~2-4GB of memory. The async generator uses constant memory regardless of total count.

---

### PERF-007: Missing Chunked Processing for Large Uploads

**Severity:** Important

**Bad Pattern:**
```python
async def upload_results(self, results: list[dict], key: str):
    # Serializes everything into one giant blob in memory
    data = orjson.dumps(results)
    await self.storage.upload_bytes(data, key=key)
```

**Good Pattern:**
```python
import tempfile
import orjson

async def upload_results(self, results: list[dict], key: str):
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False) as f:
        f.write(b"[")
        for i, record in enumerate(results):
            if i > 0:
                f.write(b",")
            f.write(orjson.dumps(record))
        f.write(b"]")
        tmp_path = f.name
    await self.storage.upload_file(source=tmp_path, key=key)
    os.unlink(tmp_path)
```

**Expected Performance Impact:** Serializing 1M records at once can require 2-3x the data size in peak memory. Streaming to a temp file keeps memory proportional to a single record, then uploads using multipart streaming.

---

## Serialization

### PERF-008: Using `json` Instead of `orjson`

**Severity:** Important

**Bad Pattern:**
```python
import json

def serialize_assets(self, assets: list[dict]) -> bytes:
    return json.dumps(assets).encode("utf-8")

def deserialize_assets(self, data: bytes) -> list[dict]:
    return json.loads(data.decode("utf-8"))
```

**Good Pattern:**
```python
import orjson

def serialize_assets(self, assets: list[dict]) -> bytes:
    return orjson.dumps(assets)

def deserialize_assets(self, data: bytes) -> list[dict]:
    return orjson.loads(data)
```

**Expected Performance Impact:** Per ADR-0004, `orjson` is 8-9x faster than stdlib `json` for serialization and 3-4x faster for deserialization. For a 100MB payload, this reduces serialization from ~4 seconds to ~0.5 seconds.

**Scoping guidance — only flag when:**
1. The call is inside a loop processing collections of 100+ items
2. The call is in a `@task` method, activity hot path, or Temporal interceptor
3. The result is assigned to bytes / written directly to storage or network

**Exclude (do NOT flag):**
- Config file parsing (one-time startup cost)
- Test utilities and fixtures
- Log message formatting
- Small dict serialization (< 10 keys, called infrequently)
- Any location where callers expect `str` return — `orjson.dumps` returns `bytes`, not `str`. Migrating would break the API contract unless all callers are updated.

---

### PERF-009: Unnecessary Serialize/Deserialize Round-Trips

**Severity:** Important

**Bad Pattern:**
```python
async def transform_and_store(self, asset: Asset) -> str:
    # Serialize to JSON, then deserialize back to dict, then serialize again
    json_str = asset.model_dump_json()
    as_dict = json.loads(json_str)  # Unnecessary round-trip
    as_dict["extra_field"] = "value"
    final_json = json.dumps(as_dict)
    await self.storage.upload_bytes(final_json.encode(), key="output.json")
    return "output.json"
```

**Good Pattern:**
```python
async def transform_and_store(self, asset: Asset) -> str:
    as_dict = asset.model_dump()  # Direct to dict, no JSON step
    as_dict["extra_field"] = "value"
    final_bytes = orjson.dumps(as_dict)
    await self.storage.upload_bytes(final_bytes, key="output.json")
    return "output.json"
```

**Expected Performance Impact:** Each unnecessary serialize/deserialize round-trip adds ~2x the serialization cost. For 100,000 assets, eliminating one round-trip saves 2-4 seconds of pure CPU time.

---

## N+1 Patterns

### PERF-010: Fetching Related Items One-by-One

**Severity:** Critical

**Bad Pattern:**
```python
async def enrich_tables(self, tables: list[TableAsset]) -> list[TableAsset]:
    for table in tables:
        # One API call per table to get columns - N+1 query pattern
        columns = await self.api_client.get_columns(table.qualified_name)
        table.columns = columns
    return tables
```

**Good Pattern:**
```python
async def enrich_tables(self, tables: list[TableAsset]) -> list[TableAsset]:
    # Batch fetch all columns in one call
    qualified_names = [t.qualified_name for t in tables]
    all_columns = await self.api_client.get_columns_batch(qualified_names)

    column_map = {}
    for col in all_columns:
        column_map.setdefault(col.table_qualified_name, []).append(col)

    for table in tables:
        table.columns = column_map.get(table.qualified_name, [])
    return tables
```

**Expected Performance Impact:** For 1000 tables with average 50ms API latency, sequential fetching takes 50 seconds. A single batch call takes ~200ms. This is a 250x improvement.

---

## Missing Timeouts

### PERF-011: HTTP Calls Without Timeout

**Severity:** Critical

**Bad Pattern:**
```python
async def query_source(self, query: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        # Default timeout is None in some configurations
        response = await client.post(
            f"{self.base_url}/query",
            json={"sql": query},
        )
        return response.json()
```

**Good Pattern:**
```python
async def query_source(self, query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(
        connect=10.0,
        read=300.0,   # Long queries may take time
        write=10.0,
        pool=10.0,
    )) as client:
        response = await client.post(
            f"{self.base_url}/query",
            json={"sql": query},
        )
        return response.json()
```

**Expected Performance Impact:** A hanging connection without a timeout permanently consumes a worker slot. With 10 worker slots, 10 hanging connections bring the entire worker to a standstill. Timeouts ensure bounded resource consumption.

---

### PERF-012: Database Queries Without Statement Timeout

**Severity:** Important

**Bad Pattern:**
```python
async def fetch_schema_info(self, conn, schema: str) -> list[dict]:
    # No statement timeout - a bad query can run for hours
    result = await conn.execute(
        "SELECT * FROM information_schema.columns WHERE table_schema = :schema",
        {"schema": schema},
    )
    return [dict(row) for row in result]
```

**Good Pattern:**
```python
async def fetch_schema_info(self, conn, schema: str) -> list[dict]:
    # Set statement timeout to prevent runaway queries
    await conn.execute("SET statement_timeout = '120s'")
    try:
        result = await conn.execute(
            "SELECT * FROM information_schema.columns WHERE table_schema = :schema",
            {"schema": schema},
        )
        return [dict(row) for row in result]
    finally:
        await conn.execute("RESET statement_timeout")
```

**Expected Performance Impact:** A runaway query against a customer's database holds a connection from the pool and consumes server-side resources indefinitely. Statement timeouts cap query execution at a known upper bound.

---

## Expensive Logging

### PERF-013: Eager Evaluation in Log Statements

**Severity:** Minor

**Bad Pattern:**
```python
import logging

logger = logging.getLogger(__name__)

async def process_batch(self, batch: list[Asset]):
    # f-string and model_dump() evaluate EVEN IF debug logging is disabled
    logger.debug(f"Processing batch of {len(batch)} assets: {[a.model_dump() for a in batch]}")
    await self._do_process(batch)
```

**Good Pattern:**
```python
import logging

logger = logging.getLogger(__name__)

async def process_batch(self, batch: list[Asset]):
    # Lazy: arguments only evaluated if DEBUG level is enabled
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Processing batch of %d assets: %s", len(batch), [a.qualified_name for a in batch])
    await self._do_process(batch)
```

**Expected Performance Impact:** `model_dump()` on 1000 Pydantic models costs ~50ms. In a tight loop with INFO-level logging (debug disabled), this wasted computation adds up to seconds per workflow run.

---

## Import Performance

### PERF-014: Heavy Module Imports at Top Level

**Severity:** Important

**Bad Pattern:**
```python
# At module top level - imported even if never used in this activity
import duckdb          # ~200ms import time
import pandas as pd    # ~300ms import time
import daft            # ~150ms import time

@activity.defn
async def analyze_query_log(self, log_path: str) -> dict:
    df = duckdb.read_parquet(log_path)
    ...
```

**Good Pattern:**
```python
@activity.defn
async def analyze_query_log(self, log_path: str) -> dict:
    # Lazy import: only pay the cost when the activity is actually invoked
    import duckdb

    df = duckdb.read_parquet(log_path)
    ...
```

**Expected Performance Impact:** Eager imports of heavy libraries add 500-700ms to worker startup time. In auto-scaling scenarios where workers start/stop frequently, this adds significant latency to the first activity execution.

**Exclude (do NOT flag):**
- Modules whose **sole purpose** is to use the heavy dependency (e.g., `transformers/query/__init__.py` exists to provide daft-based transformation — every class uses daft). Anyone importing the module needs the dependency.
- Modules named after the dependency (e.g., `duckdb_utils.py`, `parquet.py`) — the dependency is expected.
- Only flag when the heavy import is in a **general-purpose module** that has consumers who never use the heavy-dep code path.

*Lesson from 2026-04-16 run: Gate 1 correctly killed PERF-014 findings for `transformers/query/__init__.py`, `transformers/atlas/__init__.py`, and `incremental/column_extraction/backfill.py` — all modules whose entire purpose is the heavy dependency.*

---

## File I/O

### PERF-015: Synchronous Large File Operations

**Severity:** Important

**Bad Pattern:**
```python
async def download_and_process(self, key: str) -> int:
    # Synchronous download blocks event loop for potentially minutes
    local_path = "/tmp/download.parquet"
    self.s3_client.download_file(self.bucket, key, local_path)

    with open(local_path, "rb") as f:
        data = f.read()  # Reads entire file into memory synchronously
    return len(data)
```

**Good Pattern:**
```python
from application_sdk.execution.heartbeat import run_in_thread

async def download_and_process(self, key: str) -> int:
    local_path = "/tmp/download.parquet"

    def _download():
        self.s3_client.download_file(self.bucket, key, local_path)

    await run_in_thread(_download)

    # Stream the file to avoid loading entirely into memory
    size = 0
    def _count():
        nonlocal size
        with open(local_path, "rb") as f:
            while chunk := f.read(8192):
                size += len(chunk)

    await run_in_thread(_count)
    return size
```

**Expected Performance Impact:** A 1GB file download takes 10-60 seconds depending on network. Blocking the event loop for that duration stalls all other activities. Using `run_in_thread()` keeps the loop free.

---

## Concurrency

### PERF-016: Sequential Operations That Could Be Parallel

**Severity:** Important

**Bad Pattern:**
```python
async def preflight_checks(self, config: ConnectionConfig) -> list[CheckResult]:
    results = []
    # Each check is independent but run sequentially
    results.append(await self.check_connectivity(config))
    results.append(await self.check_permissions(config))
    results.append(await self.check_schema_access(config))
    results.append(await self.check_warehouse_status(config))
    return results
```

**Good Pattern:**
```python
import asyncio

async def preflight_checks(self, config: ConnectionConfig) -> list[CheckResult]:
    results = await asyncio.gather(
        self.check_connectivity(config),
        self.check_permissions(config),
        self.check_schema_access(config),
        self.check_warehouse_status(config),
    )
    return list(results)
```

**Expected Performance Impact:** If each check takes 2 seconds, sequential execution takes 8 seconds. Parallel execution completes in ~2 seconds (bounded by the slowest check). 4x improvement for preflight.

---

### PERF-017: Thread Pool Exhaustion from Excessive `run_in_thread()`

**Severity:** Important

**Bad Pattern:**
```python
from application_sdk.execution.heartbeat import run_in_thread

async def extract_all_tables(self, tables: list[str]) -> list[dict]:
    tasks = []
    for table in tables:
        # Spawns one thread per table - 10,000 tables = 10,000 threads
        tasks.append(run_in_thread(lambda t=table: self.sync_fetch(t)))
    return await asyncio.gather(*tasks)
```

**Good Pattern:**
```python
import asyncio
from application_sdk.execution.heartbeat import run_in_thread

async def extract_all_tables(self, tables: list[str], concurrency: int = 20) -> list[dict]:
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_fetch(table: str) -> dict:
        async with semaphore:
            return await run_in_thread(lambda t=table: self.sync_fetch(t))

    tasks = [bounded_fetch(table) for table in tables]
    return await asyncio.gather(*tasks)
```

**Expected Performance Impact:** The default thread pool in Python has a limited number of workers (usually `min(32, os.cpu_count() + 4)`). Spawning thousands of `run_in_thread()` calls queues them all, creating massive memory overhead and scheduling contention. A semaphore bounds actual concurrency to a sustainable level.

**Exclude (do NOT flag):**
- `ThreadPoolExecutor` created per-method-call (e.g., `with ThreadPoolExecutor() as pool:` inside a function) when the function is called a **low number of times** per workflow (e.g., `run_query()` called a handful of times). Threads are lazily spawned and the `with` block properly cleans up. Only flag when the executor is created inside a **loop** or in a function called hundreds/thousands of times.
- `run_in_executor(None, ...)` with the default executor — this uses Python's shared default pool and is fine for occasional blocking calls.

*Lesson from 2026-04-16 run: PR #1407 was closed after human review found that `BaseSQLClient.run_query()` creates a ThreadPoolExecutor per call, but the call frequency is low (a few queries per workflow). The proposed fix (reusing the heartbeat executor) would have polluted a pool meant for a different purpose.*
