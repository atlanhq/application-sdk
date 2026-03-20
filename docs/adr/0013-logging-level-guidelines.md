# ADR-0013: Logging Level Guidelines

## Status
**Accepted**

## Context

The SDK uses structlog with JSON output, forwarded to an OpenTelemetry collector and stored in Loki (or equivalent) for querying by correlation ID, workflow ID, or structured field values.

Without a shared definition of what belongs at each level, developers make inconsistent choices: DEBUG entries appear in production INFO streams (noisy, expensive), transient issues get logged at ERROR (drowning real alerts), and non-recoverable failures are sometimes swallowed entirely.

This ADR codifies the level conventions for all apps built on the SDK.

## Decision

Use exactly four levels — **DEBUG**, **INFO**, **WARNING**, **ERROR** — with the definitions below. Never use `CRITICAL` (use `ERROR` instead; process termination is communicated through exit codes and Temporal workflow failure, not log level).

---

### DEBUG

**Purpose**: Internal state that helps a developer trace execution step-by-step when actively debugging. Disabled in production by default (`ATLAN_LOG_LEVEL=INFO`).

**Use when:**
- Entering or exiting a non-trivial internal function with its key arguments/results
- Cache hits/misses and resolution steps
- Per-item progress within a tight loop (e.g., each record in a batch)
- Credential resolution intermediates (fetched, parsed, refreshed)
- Retry attempt details (attempt number, backoff duration)

```python
self.logger.debug("credential cache hit", credential_name=ref.name)
self.logger.debug("Atlan async client created", credential_type=type(cred).__name__)
self.logger.debug("Type discovered", type_name=typename, asset_count=count)
```

**Performance note**: Avoid computing expensive values just to pass to `logger.debug()`:
```python
# BAD — serializes the full record even when DEBUG is disabled
self.logger.debug("record", data=json.dumps(record))

# GOOD — skip expensive work when not needed
if self.logger.isEnabledFor(logging.DEBUG):
    self.logger.debug("record", data=json.dumps(record))
```

**Anti-patterns:**
- Do NOT log every iteration of a hot inner loop at DEBUG — use sampling or batch-level summaries
- Do NOT include sensitive credential values, even at DEBUG

---

### INFO

**Purpose**: Milestones a human operator cares about when monitoring a healthy system. The primary signal in production.

**Use when:**
- App/worker lifecycle events (starting, connected, shutting down)
- Major phase transitions within a run (extraction started, transform complete, loading N records)
- Configuration summary at startup
- Significant counts or durations at completion (records processed, elapsed time)
- Child app calls starting and completing

```python
self.logger.info("Connecting to Temporal", host=temporal_host)
self.logger.info("Extraction complete", records=count, elapsed_s=round(elapsed, 2))
self.logger.info("Shutdown signal received")
```

**Anti-patterns:**
- Do NOT log at INFO inside a loop that runs per-record (use DEBUG or batch summaries)
- Do NOT duplicate context in prose: `logger.info(f"Processing {n} records")` → `logger.info("Processing records", count=n)`

---

### WARNING

**Purpose**: Something unexpected happened, but the operation can continue. Requires human attention but not immediate action.

**Use when:**
- A recoverable error occurred and a fallback was used
- A retryable condition was encountered before a retry
- Configuration is missing and a safe default is applied
- A non-critical background operation failed (e.g., checkpoint rotation)
- A deprecated code path was taken

**Always include `exc_info=True`** when an exception is being swallowed or downgraded:
```python
self.logger.warning("Failed to rotate checkpoint", exc_info=True)
self.logger.warning("Redis unavailable, using local capacity pool", exc_info=True)
```

**Anti-patterns:**
- Do NOT use WARNING for expected conditions handled cleanly (e.g., cache miss is DEBUG)
- Do NOT omit `exc_info=True` when downgrading a caught exception — the stack trace is the only way to reproduce the root cause
- Do NOT log WARNING then immediately raise — pick one

---

### ERROR

**Purpose**: An operation failed and could not recover. The failure is significant, but the process is still running.

**Use when:**
- A single item/batch failed and the run will continue processing other items
- A required external call failed after all retries are exhausted
- An unrecoverable state was detected for a specific unit of work (but not the whole process)

**Always include `exc_info=True`** (or use `logger.exception()` which sets it automatically):
```python
self.logger.error("Batch failed, continuing", batch_num=n, exc_info=True)
self.logger.error("Request failed", path=request.path, status_code=500, exc_info=True)
```

**`logger.exception()` vs `logger.error(..., exc_info=True)`:**
Use `logger.exception()` only inside an `except` block where you are not re-raising:
```python
# GOOD — terminal handling
try:
    run_main(config)
except Exception as e:
    logger.exception("Fatal error", error=str(e))
    sys.exit(1)

# GOOD — re-raising after logging
try:
    result = do_work()
except SomeError:
    logger.error("Work failed, will retry", exc_info=True)
    raise
```

**Anti-patterns:**
- Do NOT use ERROR for expected conditions that are handled cleanly
- Do NOT log ERROR and then swallow the exception silently — if an error is logged, either raise or treat it as the final word on that unit of work

---

## Structured Fields over String Interpolation

Always pass context as keyword arguments, never interpolate into the message string:

```python
# BAD — context is buried in the string, unsearchable
self.logger.info(f"Loaded {count} records in {elapsed:.2f}s for app {app_name}")

# GOOD — context is structured, queryable by field name
self.logger.info("Records loaded", count=count, elapsed_s=round(elapsed, 2), app=app_name)
```

This is critical because the OTEL/Loki pipeline indexes structured fields for fast querying. String-embedded values are not indexed.

## Never Swallow Exceptions Silently

**NEVER silently swallow exceptions** unless they are 100% expected (e.g., checking if a file exists). At minimum, log at WARNING with `exc_info=True`.

```python
# FORBIDDEN — makes debugging nearly impossible
try:
    do_something()
except Exception:
    pass

# MINIMUM acceptable
try:
    do_something()
except Exception:
    self.logger.warning("Failed to do something", exc_info=True)
```

## Level Summary

| Level | Production default | Includes stack trace | Typical volume |
|-------|-------------------|---------------------|----------------|
| DEBUG | Off (`ATLAN_LOG_LEVEL=INFO`) | No (add manually if needed) | Very high — per-item |
| INFO | On | No | Low — per-phase |
| WARNING | On | Yes (`exc_info=True`) | Very low — per-anomaly |
| ERROR | On | Yes (`exc_info=True` or `logger.exception()`) | Near-zero — per-failure |

## Consequences

**Positive:**
- Consistent level semantics across all apps
- Production INFO streams remain low-volume and scannable
- ERROR entries reliably include stack traces, reducing MTTR
- Structured fields make Grafana/Loki dashboards and alerts easy to build

**Negative:**
- Developers must actively decide the right level — no automatic classification

## Implementation

No framework changes required — these are conventions enforced in code review:

1. Does any new `logger.warning()` or `logger.error()` that catches an exception include `exc_info=True`?
2. Are INFO calls inside tight loops? (Use DEBUG or batch summaries instead.)
3. Are structured fields used instead of f-string interpolation?

Log level is controlled by `ATLAN_LOG_LEVEL` env var → `logLevel` in `helm/atlan-app/values.yaml` → defaults to `INFO`. Debug logs are never visible in production unless explicitly enabled.
