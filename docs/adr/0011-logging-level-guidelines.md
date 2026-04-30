# ADR-0011: Logging Level Guidelines

## Status
**Accepted**

## Context

The SDK uses **loguru** (via `AtlanLoggerAdapter`) with structured output, forwarded to an OpenTelemetry collector and stored in Loki (or equivalent) for querying by correlation ID, workflow ID, or structured field values.

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
self.logger.debug("credential cache hit name=%s", ref.name)
self.logger.debug("Atlan async client created credential_type=%s", type(cred).__name__)
self.logger.debug("Type discovered type_name=%s asset_count=%s", typename, count)
```

**Performance note**: Avoid computing expensive values just to pass to `logger.debug()`. The project logger is loguru-backed and does not expose stdlib's `isEnabledFor()`. Use lazy evaluation via `opt(lazy=True)` instead:
```python
# BAD — serializes the full record even when DEBUG is disabled
self.logger.debug("record", data=json.dumps(record))

# GOOD — loguru evaluates the lambda only when DEBUG is enabled
self.logger.opt(lazy=True).debug("record {data}", data=lambda: json.dumps(record))
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
self.logger.info("Connecting to Temporal host=%s", temporal_host)
self.logger.info("Extraction complete records=%s elapsed_s=%.2f", count, round(elapsed, 2))
self.logger.info("Shutdown signal received")
```

**Anti-patterns:**
- Do NOT log at INFO inside a loop that runs per-record (use DEBUG or batch summaries)
- Do NOT interpolate values via f-string: `logger.info(f"Processing {n} records")` → `logger.info("Processing records count=%s", n)`

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

**Always include `exc_info=True`:**
```python
self.logger.error("Batch failed, continuing; batch_num=%s", n, exc_info=True)
self.logger.error("Request failed; path=%s status=%s", request.path, 500, exc_info=True)
```

**Never use `logger.exception()`** — always use `logger.error(..., exc_info=True)` instead. This keeps the level mental model clean: `{debug, info, warning, error, critical}` with no aliases.

```python
# GOOD
try:
    run_main(config)
except Exception as e:
    logger.error("Fatal error: %s", e, exc_info=True)
    sys.exit(1)

# GOOD — re-raising after logging
try:
    result = do_work()
except SomeError as e:
    logger.error("Work failed, will retry: %s", e, exc_info=True)
    raise
```

**Anti-patterns:**
- Do NOT use ERROR for expected conditions that are handled cleanly
- Do NOT log ERROR and then swallow the exception silently — if an error is logged, either raise or treat it as the final word on that unit of work

---

## %-style over f-strings

Always use `%`-style placeholders in log message strings. Never use f-strings or `str.format()`:

```python
# BAD — f-string; values are not indexable and may include sensitive data
self.logger.info(f"Loaded {count} records in {elapsed:.2f}s for app {app_name}")

# GOOD — %-style; values are rendered lazily and the pattern is grep-able
self.logger.info("Loaded %s records in %.2fs for app %s", count, elapsed, app_name)
```

Do **not** pass extra per-field kwargs (e.g. `logger.info("msg", count=n)`). Only Temporal context kwargs (`workflow_id`, `run_id`, etc.) are promoted to indexed fields in the OTEL/Loki pipeline — all other kwargs land in an opaque JSON blob. Embed differentiating context in the message body using `%-style` so it is always visible in log output regardless of pipeline configuration.

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
| ERROR | On | Yes (`exc_info=True`) | Near-zero — per-failure |

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
3. Are `%-style` placeholders used in the message body instead of f-strings or kwargs?

Log level is controlled by the `ATLAN_LOG_LEVEL` env var and defaults to `INFO`. Debug logs are never visible in production unless explicitly enabled.
