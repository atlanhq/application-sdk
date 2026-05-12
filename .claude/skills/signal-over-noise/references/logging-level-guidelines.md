# Logging Level Guidelines

Used by the signal-over-noise skill (Phase 2: Tune). Universal guidance on logging levels,
when to use each, and anti-patterns. Applies regardless of framework (stdlib logging, structlog,
loguru, or custom wrappers).

Detectable anti-patterns with grep patterns and fix templates are in `logging-patterns.md`
(same directory).

---

## The Four Levels

Use exactly four levels: **DEBUG**, **INFO**, **WARNING**, **ERROR**.

**Never use CRITICAL** — if the process must die, use ERROR and then exit. CRITICAL is not a
meaningful distinction in distributed systems where every service failure is "critical". It
creates confusion about severity tiers without adding value.

**Never use TRACE** — DEBUG already serves this purpose. Sub-DEBUG levels are not portable
across frameworks and create fragmentation.

---

## Level Definitions

### DEBUG

**What it is:** Internal state captured for step-by-step debugging. Invisible in production by default.

**Use when:**
- Entering/exiting a non-trivial function with its arguments and result
- Loop iteration state (every N iterations, not every item)
- Cache hit/miss decisions
- Branching logic decisions ("chose path X because condition Y")
- Intermediate computation values

**Do not use when:**
- The same information is always emitted at INFO
- The log line would appear millions of times per second in a healthy system

**Example — good:**
```python
logger.debug("page fetched", page=offset // page_size, row_count=len(rows))
```

**Example — bad:**
```python
for row in rows:
    logger.debug("processing row", row_id=row["id"])  # millions of log lines
```

### INFO

**What it is:** Lifecycle milestones that an operator monitors in a healthy, working system.

**Use when:**
- Application startup and shutdown (with key configuration values)
- Phase transitions (extraction started, extraction complete, load started)
- Periodic progress summaries (every N records, not every record)
- External dependency connections established
- Scheduled jobs triggered and completed

**Do not use when:**
- Processing every individual record, item, or row — use DEBUG
- Reporting "nothing to do" or empty results — these are DEBUG
- Inside tight loops — should be DEBUG or a batched summary

**Example — good:**
```python
logger.info("extraction complete", record_count=total, duration_s=elapsed)
```

**Example — bad:**
```python
for asset in assets:
    logger.info("processing asset", name=asset.name)  # use DEBUG
```

### WARNING

**What it is:** Something unexpected happened, but the system recovered and continued. An
operator should be aware but does not need to act immediately.

**Use when:**
- A retry succeeded after a transient failure
- A fallback was used because a preferred path failed
- A deprecated code path was invoked
- A rate limit was hit and the system backed off
- An optional external dependency is unavailable and the system degraded gracefully

**Always include `exc_info=True`** when downgrading an exception to WARNING — the stack trace
is essential for diagnosing what triggered the warning.

**Do not use when:**
- You are immediately re-raising the exception — that's an ERROR, or just re-raise without logging
- The condition is expected in normal operations — use DEBUG

**Example — good:**
```python
except TransientConnectionError:
    logger.warning("connection failed, retrying", attempt=attempt, exc_info=True)
```

**Example — bad:**
```python
except Exception as e:
    logger.warning("something went wrong")  # no exc_info, no context
    raise  # you're raising anyway — either use ERROR or just re-raise
```

### ERROR

**What it is:** A unit of work failed and cannot be recovered. The system is still running
but this operation is done.

**Use when:**
- An exception is caught at a terminal handler and the operation is abandoned
- A required external dependency is permanently unavailable for this request
- Data validation failed and the record must be skipped or the job aborted
- A workflow step failed after all retries were exhausted

**Always include `exc_info=True`** (or use `logger.exception()` which does this automatically).

**Do not use when:**
- The condition is expected (e.g., "entity not found" when checking existence) — use DEBUG or INFO
- You are going to retry — use WARNING, then ERROR after exhausting retries

**Example — good:**
```python
except Exception:
    logger.error("failed to load batch", batch_id=batch_id, exc_info=True)
    return LoadResult(failed=True)
```

---

## Context in Log Messages — Always Use %-style

**Rule: always embed context in the message body using %-style formatting. kwargs in application
log calls are an anti-pattern.** The only accepted kwarg is `exc_info=True`.

### Why kwargs are an anti-pattern in application code

In typical OpenTelemetry→Grafana/ClickHouse pipelines (and similar), the log record has:
- **Top-level indexed fields:** a small fixed set — `level`, `service.name`, plus framework
  context fields explicitly promoted by the logging adapter (e.g. Temporal's `workflow_id`,
  `run_id`, `activity_type`, `task_queue`, `attempt`)
- **`LogAttributes` (JSON blob):** all other kwargs land here as a serialised dict — present but
  NOT individually indexed; requires JSON extraction to query
- **`Body`:** the log message string — text-searchable at full scan speed

**Framework context is auto-injected.** The logging adapter automatically propagates all
Temporal/platform context (workflow_id, run_id, activity_type, task_queue, attempt, trace_id,
correlation_id, etc.) onto every log record via its `process()` method. Application code never
needs to pass these fields manually — they are always there.

**Application kwargs land in the JSON blob.** Any kwargs that are not framework-propagated
fields end up in an unindexed JSON blob, not as top-level queryable columns. They require slow
JSON extraction to filter on and are invisible when scanning the raw log stream.

**The message body is always available.** The `Body` field is text-searchable at full scan
speed. Context embedded in the message is immediately readable in any log viewer, without
needing to expand structured fields.

### The correct pattern: %-style in the message body

Always embed context directly in the message string using %-style formatting:

```python
# Good — context visible at a glance, no JSON blob needed
logger.info("loaded %d records from %s", count, table)
logger.warning("connection to %s:%d failed on attempt %d, retrying", host, port, n, exc_info=True)
logger.error("discovery error for connector %s", connector_name, exc_info=True)

# Good — literal string when values are known at the call site
logger.info("published asset_updated event to topic application_events")
```

### What NOT to do

```python
# Bad — context buried in JSON blob, invisible in log stream
logger.info("records loaded", count=count, table=table)

# Bad — framework context already injected by adapter; this is a no-op at best
logger.info("activity started", workflow_id=workflow_id, activity_type=activity_type)

# Bad — f-string breaks log grouping (unique message per call) and evaluates eagerly
logger.info(f"loaded {count} records from {table}")

# Bad — concatenation has the same problems as f-string
logger.info("loaded " + str(count) + " records")
```

### The one accepted kwarg: `exc_info=True`

`exc_info=True` is always correct in except blocks — it attaches the stack trace and is not
application data:

```python
except ConnectionError:
    logger.warning("connection to %s:%d failed, retrying", host, port, exc_info=True)
```

### f-strings and concatenation are also problematic

Even when not using kwargs, **avoid f-strings and concatenation** — they create a unique message
string per call, breaking log grouping and aggregation:

```python
# Bad
logger.info(f"published {event_type} event to {topic}")
logger.info("loaded " + str(count) + " records")

# Good — %-style keeps a static template for grouping
logger.info("published %s event to %s", event_type, topic)
```

### Summary rule

> **Always embed context in the message body via %-style.** kwargs in application log calls
> are an anti-pattern — framework context is auto-injected by the adapter, and all other kwargs
> land in an unindexed JSON blob. The only exception is `exc_info=True`.

---

## `logger.exception()` vs `logger.error(..., exc_info=True)`

These are functionally equivalent — both log at ERROR level with the current exception's traceback.

**Use `logger.exception()`** only at **terminal catch sites where you are NOT re-raising** the
exception. It signals "this is where the exception stops".

**Use `logger.error(..., exc_info=True)`** when you want to embed additional context in the
message alongside the traceback.

**Use `logger.warning(..., exc_info=True)`** when downgrading a caught exception to a warning
(and not re-raising).

**Never use** `logger.exception()` and then re-raise — you'll get a duplicate traceback downstream.

```python
# Terminal handler — use .exception()
except Exception:
    logger.exception("batch load failed for batch %s", batch_id)
    return FailedResult()

# Embedding context in message — use exc_info=True
except ConnectionError:
    logger.error("connection to %s:%d failed", host, port, exc_info=True)
    raise

# Downgrading to warning — use exc_info=True
except TransientError:
    logger.warning("transient error on attempt %d, retrying", n, exc_info=True)
```

---

## Performance: Lazy Evaluation

Avoid eagerly computing values that are only used if the log line is emitted.

**Bad — argument always evaluated even when DEBUG is off:**
```python
logger.debug("state dump: %s", json.dumps(large_object))  # json.dumps always runs
```

**Good — guard expensive computation:**
```python
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("state dump", state=large_object)
```

**Good — stdlib %-style is lazy (message formatted only if level is enabled):**
```python
logger.debug("processing %s", record_id)  # format string only evaluated if DEBUG enabled
```

**Critical clarification:** None of the three frameworks (stdlib, structlog, loguru) provide
lazy argument evaluation. In all three, `expensive_call()` in `logger.debug("msg", x=expensive_call())`
runs at call time regardless of log level. The only protection is an explicit `isEnabledFor`
guard. Structlog and loguru have zero lazy evaluation at any layer.

loguru provides `logger.opt(lazy=True)` which accepts callables:
```python
logger.opt(lazy=True).debug("data: {}", lambda: json.dumps(huge))
```
But if you forget `lazy=True` and pass a lambda, the output will be the lambda's repr, not its
result.

---

## Security: Never Log Credential Values

**Never log:** passwords, tokens, API keys, secrets, connection strings with credentials,
bearer tokens, private keys.

**Always acceptable:** credential names, credential types, whether a credential was found/missing.

```python
# BAD — logs the actual secret
logger.debug("using token", token=api_token)
logger.info("connecting", connection_string=conn_str_with_password)

# GOOD — logs the name or type only
logger.debug("credential loaded", credential_name="snowflake-prod-token")
logger.info("connecting", host=host, credential_type="service_account")
```

This applies regardless of log level — credential values must never appear in any log output,
even DEBUG.

---

## Anti-patterns per Level (Summary)

These are summarized here for reference. Full detectable patterns with grep queries are in
`logging-patterns.md` (same directory).

| Level | Anti-pattern |
|-------|-------------|
| DEBUG | Logging every item in a tight loop without sampling or batching |
| DEBUG | Expensive argument computation without an `isEnabledFor` guard |
| INFO | Per-item log lines that should be DEBUG |
| INFO | "Nothing happened" messages that add noise to operator dashboards |
| WARNING | Missing `exc_info=True` when catching an exception |
| WARNING | Logging a warning immediately before re-raising (redundant with downstream ERROR) |
| ERROR | Expected, handled conditions logged as ERROR (raises alert fatigue) |
| ERROR | Error logged and then exception silently swallowed (hides failures) |
| Any | f-string or concatenation in log message (breaks log grouping; use %-style or literal) |
| Any | kwargs in application log calls other than `exc_info=True` (buries context in JSON blob; framework context is auto-injected by the adapter; embed other context in message body via %-style) |
| Any | Credential values embedded in log output (security violation) |
| Any | `print()` in production code instead of structured logging |
| Any | `logger.critical()` usage |

---

## Framework Awareness Notes

These are non-grep-detectable runtime risks. The skill does not produce findings for them, but
they are documented here for developer awareness when reviewing logging code.

### fork() + Logging = Potential Deadlock

Forking a process while another thread holds a logging handler lock creates a permanent deadlock
in the child process. The child inherits a copy of the locked `RLock`, but the thread that held
the lock does not exist in the child — so the lock can never be released.

**Affected:** Python < 3.12 when using `multiprocessing`, `os.fork()`, gunicorn prefork mode,
celery prefork worker pool, or any code that forks after importing the `logging` module.

**Python 3.12+** mitigates this via `os.register_at_fork()` which reinitializes logging locks
in the child process. Older versions are vulnerable.

**Safe patterns:**
- Configure logging AFTER forking (in the child)
- Use `QueueHandler` / `QueueListener` to serialize all logging through a single non-forked thread
- Switch to gunicorn's gthread or gevent worker class instead of prefork

### Logging in Signal Handlers

Signal handlers interrupt the current thread at an arbitrary point — including mid-logging-call.
If the interrupted thread holds a logging lock, and the signal handler also tries to log, the
behavior is undefined. The module-level `logging._lock` is an `RLock` (reentrant for the same
thread), which means same-thread re-entry is safe. But if the signal handler modifies the logger
hierarchy (adds handlers, changes levels) while the main thread iterates `c.handlers` in
`callHandlers()`, you can get "dictionary changed size during iteration" or similar errors.

**Safe pattern:** Signal handlers should set a flag or write to `signal.set_wakeup_fd()`;
the main event loop reads the flag and does the logging, not the signal handler itself.

### Async Context and Thread-Local State

Both `structlog.threadlocal` and any `threading.local()` based context solution are broken in
asyncio. All async tasks run on the same thread, so thread-local context is shared across
concurrent tasks. Context set in one task overwrites context in another at every `await` point.

**Use instead:**
- `structlog.contextvars` (structlog ≥ 21.1) — uses Python's `contextvars.ContextVar`
- `loguru.contextualize()` — uses `contextvars` under the hood
- `contextvars.ContextVar` directly for custom context

---

## Configuration Convention

Log level should be configurable at runtime without code changes:

- Read from environment variable (e.g., `LOG_LEVEL`, `ATLAN_LOG_LEVEL`)
- Default to `INFO` in production
- `DEBUG` available for local development and troubleshooting
- Never hardcode `DEBUG` in production code paths

```python
import os
import logging

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
```

For structlog/loguru — configure the level on the underlying stdlib handler or on the
framework's own level filter at startup. Never set level inside application logic.
