# ADR-0011: Async-First Design and Blocking Code Pitfalls

## Status
**Accepted**

## Context

The SDK runs on Temporal, which uses an asyncio-based event loop for activity execution. Activities support automatic heartbeating — a background task that signals to Temporal "this activity is still alive." If heartbeats stop, Temporal assumes the activity is stuck and restarts it.

This works well for async code, but many real-world operations are blocking:
- File I/O (CSV parsing, JSON reading)
- Network calls via sync libraries (`requests`, `boto3`)
- CPU-bound computation
- C extensions (`pandas`, `numpy`, LMDB)

When blocking code runs directly in an activity, it blocks the event loop, preventing heartbeats from being sent. Temporal then restarts the activity even though it is actually making progress.

## Decision

We adopt an **async-first design** with a `self.task_context.run_in_thread()` helper for blocking operations. Critically, **blocking code must have its own internal timeouts** — the framework cannot safely kill threads.

```python
@task(timeout_seconds=3600, heartbeat_timeout_seconds=120)
async def process_data(self, input: ProcessInput) -> ProcessOutput:
    # GOOD — blocking HTTP call with its own timeout, runs in thread
    response = await self.task_context.run_in_thread(
        requests.get, url, timeout=30   # ← internal timeout required
    )

    # BAD — blocks the event loop, prevents heartbeats
    # response = requests.get(url, timeout=30)

    # BAD — no timeout, could hang forever
    # response = await self.task_context.run_in_thread(requests.get, url)
    return ProcessOutput(data=response.json())
```

## Options Considered

### Option 1: Thread with Framework Timeout (Rejected)

Add a timeout parameter to `run_in_thread()` that cancels the operation after a deadline.

**Why this is dangerous:**

Python threads **cannot be forcibly killed**. When a "timeout" occurs:
1. The thread continues running in the background
2. The activity may be retried (new activity, new thread)
3. Now **two threads are doing the same work**
4. Side effects conflict: duplicate writes, race conditions, data corruption

```
T=0   Activity starts, blocking_func runs in thread
T=30  "Timeout" — but thread keeps running!
T=31  Temporal retries activity, NEW thread starts blocking_func
T=45  Original thread completes, writes to database
T=50  Retry thread completes, writes AGAIN → duplicate data
```

**Rejected** — creates worse problems than it solves.

### Option 2: Async-First with Internal Timeouts (Chosen)

The blocking code itself must have timeout support. `run_in_thread()` exists purely to keep the event loop responsive for heartbeating.

```python
# The framework provides:
async def run_in_thread(self, func, *args, **kwargs):
    """Run blocking function in thread pool.
    CRITICAL: func MUST have its own timeout handling.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

# Usage — timeout is in the BLOCKING CODE:
result = await self.task_context.run_in_thread(requests.get, url, timeout=30)
result = await self.task_context.run_in_thread(db.query, sql, statement_timeout=60)
```

**Pros:**
- **Honest about limitations**: No false sense of safety
- **Simple implementation**: Just an `asyncio.to_thread` wrapper
- **No side-effect conflicts**: Code either completes or times out on its own

**Cons:**
- **Requires developer discipline**: Must add timeouts to blocking code
- **Not all libraries support timeouts**: Some may require alternatives

### Option 3: Manual Heartbeating Only (Not Chosen)

Disable auto-heartbeating entirely; developers must call `heartbeat()` manually.

**Cons:**
- **Easy to forget**: Miss a heartbeat → activity restarted
- **Granularity problem**: Can't heartbeat during a single long-running blocking call
- **More code**: Every task needs heartbeat calls

## Rationale

### Python's thread limitation is fundamental

```python
import threading

def blocking_forever():
    while True:
        pass  # No way to stop this from outside

thread = threading.Thread(target=blocking_forever)
thread.start()
# thread.stop()      — DOES NOT EXIST
# thread.kill()      — DOES NOT EXIST
# thread.terminate() — DOES NOT EXIST
```

### Async code doesn't have this problem

True async code yields to the event loop at `await` points. When cancelled, `asyncio.CancelledError` is raised cleanly. Context managers clean up properly. No orphaned operations continue.

This is why **async-first is strongly preferred**.

## Async Library Reference

| Need | Use (async) | Avoid (blocking) |
|------|------------|-----------------|
| HTTP requests | `httpx` | `requests` |
| Database access | `asyncpg`, `aiomysql` | `psycopg2`, `pymysql` |
| File I/O | `aiofiles` | `open()` |
| AWS SDK | `aiobotocore` | `boto3` |

When no async alternative exists, use `self.task_context.run_in_thread()` with an internal timeout.

## Consequences

**Positive:**
- Event loop remains responsive during blocking operations
- Auto-heartbeating works correctly
- No false sense of safety from "timeout" features that can't actually stop threads

**Negative:**
- Developers must ensure blocking code has internal timeouts
- Blocking code without timeout support requires alternatives

## Safe Patterns

```python
# HTTP with timeout
response = await self.task_context.run_in_thread(requests.get, url, timeout=30)

# Database with statement timeout
result = await self.task_context.run_in_thread(cursor.execute, sql, timeout=60)

# File I/O (generally safe — OS handles completion)
data = await self.task_context.run_in_thread(Path(path).read_bytes)
```

## Unsafe Patterns to Avoid

```python
# BAD — blocks the event loop directly
response = requests.get(url)

# BAD — run_in_thread without an internal timeout
response = await self.task_context.run_in_thread(requests.get, url)

# BAD — reading from a pipe that might never have data
data = await self.task_context.run_in_thread(pipe.read)
```
