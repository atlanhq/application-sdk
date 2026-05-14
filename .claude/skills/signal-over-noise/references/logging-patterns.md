# Logging Anti-Patterns Catalogue

Used by the signal-over-noise skill (Phase 2: Tune). Covers **detectable anti-patterns**: grep
patterns, severity, examples, fix templates, and linting rules.

Level philosophy and when-to-use guidance is in `logging-level-guidelines.md` (same directory).

Framework-agnostic — patterns apply to stdlib logging, structlog, loguru, and custom wrappers.
The skill's Phase 2 Step 2.1 discovers which framework the project uses before applying
framework-dependent classifications.

---

## Pattern Catalogue

### L1 — f-string in Log Message

**Grep:** `logger\.\w+\(f["']` (also match `log\.`, `_logger\.`, `_log\.` — use discovered variable names)

**Severity:** HIGH

**Auto-fixable:** Yes (rewrite as %-style message body)

**Description:** Using an f-string as the log message creates a unique message string per call,
breaking log grouping and aggregation. It also always evaluates eagerly.

**Fix direction: always rewrite as %-style message body.** Do not move values to kwargs —
framework context (Temporal fields, correlation IDs, etc.) is auto-injected by the logging
adapter, and all other kwargs land in an unindexed JSON blob that is invisible in the log
stream. Embed context directly in the message string using %-style formatting.

```python
# BAD — f-string, unique message per call, breaks aggregation
logger.info(f"published {event_type} event to topic {topic}")
logger.info(f"loaded {count} records from {table}")
logger.warning(f"connection to {host}:{port} failed")

# GOOD — %-style keeps a static template for grouping, embeds context in body
logger.info("published %s event to topic %s", event_type, topic)
logger.info("loaded %d records from %s", count, table)
logger.warning("connection to %s:%d failed", host, port, exc_info=True)

# ALSO GOOD — literal string when values are known at the call site
logger.info("published asset_updated event to topic application_events")
```

**Detection note:** Adjust the regex to match logger variable names discovered in Step 2.1. The
pattern `\.\w+\(f["']` catches any method call receiving an f-string literal.

---

### L2 — Inconsistent Logger Factory

**Detection:** Step 2.1 discovers the **canonical factory** (the dominant one by occurrence count). Any file using a different factory is a finding.

**Severity:** HIGH

**Auto-fixable:** Yes (import swap)

**Description:** Mixing logger factories in one codebase produces inconsistent log formats, loses structured fields, and makes log correlation harder. A file using `logging.getLogger(__name__)` while the rest of the project uses `structlog.get_logger()` will emit plain text instead of structured JSON.

**Common factories to detect in Step 2.1:**
- `structlog.get_logger(`
- `logging.getLogger(`
- `loguru.logger` (singleton, not a factory call)
- `get_logger(` (custom project wrapper)
- `getLogger(`

**Example (project uses structlog; this file uses stdlib):**
```python
# BAD — inconsistent with project convention
import logging
logger = logging.getLogger(__name__)

# GOOD — use project's canonical factory
import structlog
logger = structlog.get_logger()
```

---

### L3 — `extra={}` When Framework Expects kwargs (or vice versa)

**Grep:** `extra=\{`

**Severity:** MEDIUM

**Auto-fixable:** No (requires framework judgment)

**Description:** Whether `extra={}` is correct depends on the logging framework:
- **stdlib logging**: `extra={}` is the correct way to pass structured fields
- **structlog**: `extra={}` is usually wrong — structlog expects keyword arguments directly; `extra` is a plain dict that is not indexed as structured context
- **loguru**: `extra={}` has no special meaning — use keyword arguments

**Framework-dependent classification:**
- If `LOGGER_FRAMEWORK == stdlib`: `extra={}` is acceptable — skip or LOW
- If `LOGGER_FRAMEWORK == structlog` or `loguru`: `extra={}` is likely wrong — MEDIUM

**Structlog nesting trap:** In structlog, `extra={"count": n}` does NOT unpack into structured
fields. It becomes a single key whose value is the dict:
```json
{"event": "batch complete", "extra": {"count": 5}}
```
The data is present in the raw log but **invisible to aggregation queries** — a dashboard
filtering on `count > 100` will find nothing.

```python
# stdlib — CORRECT
logger.info("batch complete", extra={"count": n, "table": t})

# structlog — WRONG (extra nested, not indexed at top level)
logger.info("batch complete", extra={"count": n})

# structlog — CORRECT (fields at top level)
logger.info("batch complete", count=n, table=t)
```

---

### L4 — Missing `exc_info=True` in except-block log

**Grep (multiline):** `except` block containing `logger\.\(warning\|error\)` without `exc_info`

**Severity:** HIGH

**Auto-fixable:** Yes (add `exc_info=True`)

**Description:** Logging an exception without `exc_info=True` produces a message with no stack trace, making the root cause invisible. This is one of the most common debugging blockers.

**Heuristic detection:** Flag any `logger.warning(` or `logger.error(` call that appears within 5 lines of an `except` clause and does not contain `exc_info=True` or `exc_info=exc`.

```python
# BAD — no stack trace
except ConnectionError as e:
    logger.warning("connection failed")  # WHERE did it fail? Unknown.

# BAD — no stack trace; str(e) is not a stack trace
except Exception:
    logger.error("operation failed", error=str(e))

# GOOD — exc_info=True attaches the stack trace; context embedded in message via %-style
except ConnectionError:
    logger.warning("connection to %s:%d failed on attempt %d, retrying", host, port, n, exc_info=True)

# GOOD — .exception() implies exc_info=True
except Exception:
    logger.exception("operation failed for batch %s", batch_id)
```

---

### L5 — `print()` in Production Code

**Grep:** `^\s*print\(`

**Severity:** MEDIUM

**Auto-fixable:** Yes (simple cases)

**Description:** `print()` bypasses the logging framework — no level, no structured fields, no correlation IDs, no output routing. In production services, `print()` output may go to stdout unformatted, be lost entirely, or interleave with structured log lines.

**Acceptable exceptions:**
- CLI tools where stdout is the intended output (e.g., `print(result)` in a command-line script)
- Test/debug scripts explicitly not meant for production
- `if __name__ == "__main__":` blocks

**Auto-fix heuristic:** Replace `print("message")` with `logger.info("message")` — flag for human review.

```python
# BAD — production service code
def process_batch(records):
    print(f"Processing {len(records)} records")  # not observable

# GOOD
def process_batch(records):
    logger.info("processing batch", count=len(records))
```

---

### L6 — INFO Inside Tight Loop

**Detection (heuristic):** `logger\.info\(` appearing inside a `for` or `while` block

**Severity:** MEDIUM

**Auto-fixable:** No (requires judgment — need to understand loop volume)

**Description:** Per-item INFO logging in a loop that iterates over many items generates enormous log volume, drowns out meaningful signals, and can cause significant performance degradation. INFO is for lifecycle milestones, not per-item events.

**Heuristic:** Flag `logger.info(` calls indented inside a `for` or `while` loop. Step 2.3 context reading should check whether this is a clearly-bounded small loop or an unbounded/large-scale loop.

```python
# BAD — INFO per item in a potentially large loop
for asset in assets:
    logger.info("processing asset", name=asset.name)

# GOOD — DEBUG per item, INFO for summary
for asset in assets:
    logger.debug("processing asset", name=asset.name)
logger.info("batch processed", count=len(assets))
```

---

### L7 — `logger.critical()` Usage

**Grep:** `\.critical\(`

**Severity:** MEDIUM

**Auto-fixable:** Yes (replace with `.error()`)

**Description:** `CRITICAL` is not a meaningful level in distributed systems. Every service failure is "critical" from some perspective. Using it creates a false tier that confuses alerting thresholds and log aggregation. Use ERROR and handle severity through alerting rules on the downstream observability platform.

```python
# BAD
logger.critical("database unreachable — shutting down")

# GOOD
logger.error("database unreachable — shutting down", exc_info=True)
sys.exit(1)
```

---

### L8 — Unguarded Expensive DEBUG Computation

**Detection (heuristic):** `logger.debug(` with function calls in argument position (e.g., `json.dumps(`, `repr(`, `.to_dict(`)

**Severity:** LOW

**Auto-fixable:** No (requires judgment)

**Description:** Arguments to log calls are evaluated eagerly at call time, regardless of whether the log level is enabled. An expensive serialization or computation that only serves a DEBUG log line wastes CPU even when DEBUG is disabled.

**Heuristic:** Flag `logger.debug(` calls whose arguments contain known-expensive patterns: `json.dumps(`, `json.loads(`, `repr(`, `str(large_`, `.to_dict(`, `.model_dump(`.

```python
# BAD — json.dumps() runs even when DEBUG is disabled
logger.debug("payload dump", payload=json.dumps(large_object))

# GOOD — guard expensive computation
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("payload dump", payload=json.dumps(large_object))
```

---

### L9 — Warn-then-Raise Duplication

**Detection (heuristic):** `logger.warning(` or `logger.error(` within 3 lines before a `raise` statement

**Severity:** MEDIUM

**Auto-fixable:** No (requires judgment)

**Description:** Logging an error immediately before re-raising creates duplicate records in the log stream — once where it was logged, and again where it is eventually caught and handled. This inflates error counts in dashboards and makes root-cause analysis harder.

**Exception:** Acceptable to add structured context before re-raising if the caller won't have it:
```python
# ACCEPTABLE — adding context not available to caller
except DatabaseError as e:
    logger.warning("query failed", query_id=query_id, exc_info=True)
    raise  # caller logs again, but this context would be lost otherwise
```

```python
# BAD — pure duplication, no added context
except Exception:
    logger.error("operation failed", exc_info=True)
    raise  # caller will log this again

# GOOD — just re-raise, let the terminal handler log
except Exception:
    raise

# GOOD — wrap with context, then raise
except Exception as e:
    raise OperationError(f"failed during phase {phase}") from e
```

---

### L10 — Credential/Secret Values in Log Output

**Grep (case-insensitive):** `logger\.\w+\(.*(?:password|secret|token|api_key|credential|auth|bearer|private_key)` with value-like context

**Severity:** CRITICAL

**Auto-fixable:** No (security review required)

**Description:** Credentials in log output are a security vulnerability — logs are often stored in plaintext in log aggregation systems, accessible to more people than the credential store itself.

**Detection notes:**
- The grep is intentionally broad to minimize false negatives
- Step 2.3 context reading distinguishes acceptable from critical:
  - `logger.info("token refreshed", token_name=name)` — ACCEPTABLE (logging the name)
  - `logger.info("token refreshed", token=actual_token_value)` — CRITICAL
  - `logger.debug("auth header", authorization=f"Bearer {token}")` — CRITICAL
- Any finding in this category requires human security review before marking acceptable

```python
# CRITICAL — logs actual credential
logger.debug("snowflake connection", password=credentials.password)
logger.info("connecting", token=api_token, url=endpoint)

# ACCEPTABLE — logs name/type only
logger.debug("credential loaded", credential_name="snowflake-prod")
logger.info("auth configured", method="service_account", scopes=scopes)
```

---

### L11 — String Concatenation in Log Message

**Grep:** `logger\.\w+\(".*" \+` or `logger\.\w+\('.*' \+`

**Severity:** MEDIUM

**Auto-fixable:** Yes

**Description:** Like f-strings (L1), string concatenation embeds values into the message string
in a way that breaks log grouping. The fix is the same as L1: rewrite as %-style message body.

```python
# BAD
logger.info("loaded " + str(count) + " records from " + table)

# GOOD — %-style message body (all frameworks)
logger.info("loaded %d records from %s", count, table)
```

---

### L12 — %-style Formatting in Non-stdlib Logger

**Grep:** `logger\.\w+\(".*%[sdfr]` (only flag when `LOGGER_FRAMEWORK != stdlib`)

**Severity:** LOW

**Auto-fixable:** No (verify logger type first)

**Description:** `%-style` formatting is a stdlib logging feature — it performs **lazy** formatting (only evaluates the format string if the level is enabled). However, structlog and loguru do **not** support %-style substitution.

**Per-framework behavior:**

- **structlog:** The `%s` appears **literally** in log output. The positional arg (`count`) may be
  stored as `_positional_args` internally or silently dropped, depending on the processor chain.
  Either way, the substituted value is never visible in the formatted message. No error is raised.

- **loguru:** loguru uses `str.format()` for message templates. `%s` is not a `{}` placeholder,
  so `str.format()` finds nothing to substitute. The positional argument is **silently ignored
  entirely**. No error is raised. `"loaded %s records"` appears verbatim in output.

- **stdlib + `raiseExceptions=False`** (production default): Format mismatches (too many args,
  wrong types, too few args) are caught by `handleError()` and **silently swallowed** — the
  entire log message is dropped with no indication. Only visible with `raiseExceptions=True`
  (which prints a traceback to stderr but still does not propagate to the caller).

**Framework-dependent classification:**
- If `LOGGER_FRAMEWORK == stdlib`: %-style is **correct and preferred** — mark as ACCEPTABLE
- If `LOGGER_FRAMEWORK == structlog`: %-style means **literal `%s` in output** — mark as MEDIUM
- If `LOGGER_FRAMEWORK == loguru` AND call goes through `CANONICAL_FACTORY` (e.g. `get_logger()`)
  AND that factory has a %-style bridge (look for `_format_printf_args` or `msg % args` in the
  adapter implementation) → mark as ACCEPTABLE (bridge pre-formats before handing to loguru)
- If `LOGGER_FRAMEWORK == loguru` AND call uses a **direct loguru import** (`from loguru import
  logger` / `loguru.logger`) even when a bridge adapter exists elsewhere in the project → mark
  as HIGH (the bridge is never invoked; positional args are silently dropped)
- If vanilla loguru (no bridge anywhere) → mark as HIGH

```python
# structlog — WRONG (% not substituted, appears literally as "%s")
logger.info("loaded %s records", count)

# loguru — WRONG (count silently ignored, no error)
logger.info("loaded %s records", count)

# structlog — CORRECT
logger.info("records loaded", count=count)

# loguru — CORRECT (uses str.format() style)
logger.info("loaded {} records", count)
# or with structured fields:
logger.bind(count=count).info("records loaded")

# stdlib — CORRECT (lazy evaluation)
logger.info("loaded %s records", count)
```

---

## Framework Compatibility Patterns

These patterns are framework-specific. The skill's Step 2.1 discovers the logging framework and
enables only the applicable patterns. They are numbered L13–L23.

---

### L13 — stdlib `extra={}` Reserved Key Collision (CRASH)

**Grep:** `extra=\{` — then check keys against the reserved list in Step 2.3

**Severity:** CRITICAL

**Auto-fixable:** No (requires renaming the field)

**Applies to:** stdlib only

**Description:** stdlib's `Logger.makeRecord()` explicitly raises `KeyError` if any key in
`extra={}` matches a `LogRecord` attribute. This error propagates **directly to the caller**
and is NOT caught by `handleError()` — meaning it crashes your application code even when
`logging.raiseExceptions = False`.

**The 22 forbidden keys:**
`name`, `msg`, `args`, `levelname`, `levelno`, `pathname`, `filename`, `module`, `lineno`,
`funcName`, `created`, `msecs`, `relativeCreated`, `thread`, `threadName`, `process`,
`processName`, `exc_info`, `exc_text`, `stack_info`, `message`, `asctime`

The most commonly collided names in production code: `name`, `message`, `module`, `args`,
`filename`, `process`, `thread`.

```python
# CRASH — KeyError: "Attempt to overwrite 'name' in LogRecord"
logger.info("user action", extra={"name": "alice"})

# CRASH — KeyError: "Attempt to overwrite 'message' in LogRecord"
logger.info("event", extra={"message": "the payload"})

# CRASH — KeyError: "Attempt to overwrite 'args' in LogRecord"
logger.info("call", extra={"args": [1, 2, 3]})

# GOOD — use a namespaced key that doesn't collide
logger.info("user action", extra={"user_name": "alice"})
logger.info("event", extra={"event_message": "the payload"})
```

**Acceptable?** Any `extra={}` key that is NOT in the forbidden list is fine.

**Fix guidance:** Rename the offending key. Common safe pattern: prefix with an app-specific
namespace (e.g., `user_name` instead of `name`, `app_module` instead of `module`).

---

### L14 — Arbitrary kwargs in stdlib Logger (CRASH)

**Grep:** `LOGGER_VAR_PATTERN\.\w+\([^)]*,\s*\w+=` — kwargs other than `exc_info`, `extra`,
`stack_info`, `stacklevel` in stdlib log calls

**Severity:** CRITICAL

**Auto-fixable:** Yes (wrap kwargs in `extra={}`)

**Applies to:** stdlib only

**Description:** stdlib `logger.info()` and family only accept four keyword arguments:
`exc_info`, `extra`, `stack_info`, and `stacklevel`. Any other kwarg raises `TypeError`
immediately — crashing the caller. This is a very common bug when code is migrated from
structlog or loguru (which support arbitrary kwargs) to stdlib, or when a developer assumes
structlog-style kwargs work everywhere.

```python
# CRASH — TypeError: _log() got an unexpected keyword argument 'user_id'
logger.info("user logged in", user_id="alice")

# CRASH — TypeError: _log() got an unexpected keyword argument 'request_id'
logger.warning("slow response", request_id="abc", duration_ms=450)

# GOOD — wrap in extra={}
logger.info("user logged in", extra={"user_id": "alice"})
logger.warning("slow response", extra={"request_id": "abc", "duration_ms": 450})
```

**Special double-crash cases:**
- `logger.info("msg", level="DEBUG")` — `TypeError: multiple values for argument 'level'`
- `logger.info("msg", msg="other")` — `TypeError: multiple values for argument 'msg'`

**Context reading:** Flag all kwargs in stdlib log calls that are not in `{exc_info, extra,
stack_info, stacklevel}`. Do NOT flag structlog or loguru projects — kwargs are correct there.

---

### L15 — `event=` kwarg Overwrites structlog Message

**Grep:** `LOGGER_VAR_PATTERN\.\w+\(.*event=`

**Severity:** HIGH

**Auto-fixable:** No (rename the field)

**Applies to:** structlog only

**Description:** In structlog, the first positional argument to a log call is stored as the
`event` key — it IS the log message. If you pass `event=` as a keyword argument, it silently
overwrites the message with your domain value. No error is raised.

This is extremely common in event-driven systems where "event" is a meaningful domain concept.

```python
# BAD — domain_event silently replaces the log message
logger.info("processing", event=domain_event)
# Output: {"event": <DomainEvent object>, "level": "info", ...}
# The message "processing" is completely lost.

# GOOD — use a non-colliding field name
logger.info("processing", domain_event=domain_event)
logger.info("processing", event_type=domain_event.type, event_id=domain_event.id)
```

**Acceptable?** Only if intentional — e.g., `event=event.name` where you explicitly want
the event name to BE the log message. This should be clearly commented.

---

### L16 — `dictConfig` with `disable_existing_loggers=True` (the default)

**Grep:** `dictConfig\(` — then check config dict in Step 2.3

**Severity:** HIGH

**Auto-fixable:** Yes (add `"disable_existing_loggers": False`)

**Applies to:** stdlib only

**Description:** `logging.config.dictConfig()` has a `disable_existing_loggers` key that
**defaults to `True`**. When `True`, all loggers created before `dictConfig()` is called
have their `.disabled` attribute set to `True` — they silently drop all messages forever.

This is the single most common source of "why is my logging not working?" in Python. In
Django, Flask, FastAPI, and any framework where modules do `logger = logging.getLogger(__name__)`
at import time (before the app configures logging), ALL those loggers are silently disabled.

```python
# BAD — all pre-existing loggers silently disabled after this call
logging.config.dictConfig({
    "version": 1,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "DEBUG"},
    # disable_existing_loggers not set — defaults to True!
})

# GOOD — explicitly preserve existing loggers
logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,   # <-- required
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "DEBUG"},
})
```

**Context reading:** Flag any `dictConfig()` call where the config dict does not contain
`"disable_existing_loggers": False`. If the key is absent or set to `True`, flag HIGH.

---

### L17 — `basicConfig()` No-op After First Call

**Grep:** `basicConfig\(` — collect all occurrences across the codebase; flag if >1 found

**Severity:** HIGH

**Auto-fixable:** No (requires understanding call order)

**Applies to:** stdlib only

**Description:** `logging.basicConfig()` is silently ignored if the root logger already has
handlers. The second and subsequent calls do nothing — no error, no warning. This means
whichever call runs first "wins" and all subsequent calls are silently dropped.

Common in practice:
- A library calls `basicConfig()` at import time, before the application configures logging
- A function that configures logging is called multiple times (e.g., in tests)
- Both app startup code and a library call `basicConfig()`

```python
# File: some_library.py (imported before your app)
logging.basicConfig(level=logging.WARNING)  # This runs first

# File: your_app.py
logging.basicConfig(level=logging.DEBUG)    # SILENT NO-OP — library already configured
# Result: DEBUG messages never appear
```

**Fix:** Use `logging.basicConfig(force=True)` (Python 3.8+) to override existing config,
or switch to `dictConfig()` with `"disable_existing_loggers": False`.

**Context reading:** Flag the second+ occurrence in call order (by file import order heuristic).
If `basicConfig()` appears in multiple files, flag all but the earliest-imported one as HIGH.
Single calls in `if __name__ == "__main__":` blocks are acceptable.

---

### L18 — `logger.exception()` Called Outside an `except` Block

**Grep:** `\.exception\(` — then verify context in Step 2.3

**Severity:** MEDIUM

**Auto-fixable:** No

**Applies to:** all frameworks

**Description:** `logger.exception()` is equivalent to `logger.error(..., exc_info=True)`. When
called outside an `except` block, `sys.exc_info()` returns `(None, None, None)`, so the log
output appends `NoneType: None` — a meaningless traceback that actively misleads debugging.

The call itself does not crash, but the output is misleading. Use `logger.error()` when you
want to log an error without an active exception context.

```python
# BAD — outside except block, logs "NoneType: None" as the traceback
def validate(data):
    if not data:
        logger.exception("validation failed")  # misleading output

# GOOD — use logger.error() outside except blocks
def validate(data):
    if not data:
        logger.error("validation failed")

# GOOD — logger.exception() is appropriate inside except
try:
    process(data)
except ValueError:
    logger.exception("processing failed")  # correct — has exception context
```

**Context reading:** Read 5 lines before the `.exception()` call. If there is no `except`
clause in scope, flag MEDIUM.

---

### L19 — `bind()` Return Value Discarded

**Grep:** `^\s+\w+\.bind\(` (bare statement — no assignment)

**Severity:** HIGH

**Auto-fixable:** Yes (add `logger = ` assignment)

**Applies to:** structlog, loguru

**Description:** Both structlog and loguru's `bind()` method returns a **new** logger with the
bound context. The original logger is not modified. If the return value is discarded, the
binding is a silent no-op — the context is never attached to any subsequent log call.

```python
# BAD — return value discarded; request_id never attached
logger.bind(request_id="abc")    # structlog
logger.bind(request_id="abc")    # loguru
logger.info("handling request")  # request_id NOT present in output

# GOOD — capture the return value
logger = logger.bind(request_id="abc")
logger.info("handling request")  # request_id IS present

# GOOD (loguru) — use contextualize() for scope-bound context
with logger.contextualize(request_id="abc"):
    logger.info("handling request")

# GOOD (structlog) — use contextvars for async-safe binding
structlog.contextvars.bind_contextvars(request_id="abc")
```

**Context reading:** The grep matches lines where `something.bind(...)` is a bare expression
(not assigned). Confirm it is a `bind()` call on a logger variable (not an unrelated `.bind()`
method on another object type).

---

### L20 — `propagate=False` Without Handlers

**Grep:** `propagate\s*=\s*False`

**Severity:** HIGH

**Auto-fixable:** No

**Applies to:** stdlib only

**Description:** Setting `logger.propagate = False` prevents log records from bubbling up to
the parent logger's handlers. If the logger has no handlers of its own, the message falls
through to `lastResort` (WARNING+ only) or is silently dropped entirely for DEBUG/INFO.

```python
# BAD — messages from this logger silently dropped
child_logger = logging.getLogger("app.worker")
child_logger.propagate = False
# No handlers added — all messages are lost

# GOOD — add a handler before disabling propagation
child_logger = logging.getLogger("app.worker")
child_logger.addHandler(logging.StreamHandler())
child_logger.propagate = False

# GOOD — or keep propagate=True and let the root handler receive
child_logger = logging.getLogger("app.worker")
# propagate=True (default) — no explicit handler needed
```

**Context reading:** Read 10 lines around the `propagate = False` assignment. Check whether
`addHandler()` is called on the same logger object. If no handler is added, flag HIGH.

---

### L21 — `logger.remove()` Kills All Loguru Sinks

**Grep:** `logger\.remove\(\s*\)` (no arguments)

**Severity:** HIGH

**Auto-fixable:** No

**Applies to:** loguru only

**Description:** `logger.remove()` with no arguments removes ALL sinks, including the default
stderr sink that loguru installs at startup. After this call, log output goes nowhere. No
error or warning is issued.

This is commonly done intentionally to replace the default sink, but when the replacement
`logger.add()` call is conditional, fails, or is in a different code path, all logging
silently stops.

```python
# BAD — if logger.add() is never reached (exception, wrong branch, etc.)
logger.remove()
logger.add("app.log")      # If this fails or is skipped, all output is lost

# GOOD — use a specific handler ID to remove only the default sink
handler_id = logger.add("app.log")
logger.remove(0)  # Remove only the default stderr sink (ID=0)

# GOOD — add the new sink before removing the default
logger.add("app.log")
logger.remove(0)
```

**Acceptable?** `logger.remove(handler_id)` with a specific ID is fine — it only removes
that one sink. Only flag bare `logger.remove()` (no arguments).

---

### L22 — kwargs in loguru Log Calls (Silently Ignored or Consumed)

**Grep:** `LOGGER_VAR_PATTERN\.\w+\([^)]*,\s*\w+=` (log call with kwargs other than `exc_info`) — collect hits for Step 2.3

**Severity:** MEDIUM

**Auto-fixable:** Yes (move to %-style message body)

**Applies to:** loguru (and custom loguru-based adapters)

**Description:** In loguru, keyword arguments to log calls are used for `str.format()` message
interpolation — NOT stored as structured fields. If the message has no matching `{kwarg_name}`
placeholder, the kwarg is **silently ignored entirely**. It does not appear in structured output.

Beyond the loguru-specific silent-ignore problem, kwargs in application log calls are an
anti-pattern regardless: framework context (Temporal fields, correlation IDs, etc.) is
auto-injected by the logging adapter, and all other kwargs land in an unindexed JSON blob.
**The correct fix is always to embed context in the message body via %-style**, not to use
`.bind()` or `{}` placeholders.

```python
# BAD — user_id silently ignored; not in structured output
logger.info("user logged in", user_id=123)

# BAD — even with {} placeholder, value is consumed by formatting, not a queryable field
logger.info("user {user_id} logged in", user_id=123)

# BAD — .bind() stores in JSON blob, not a top-level indexed field
logger.bind(user_id=123).info("user logged in")

# GOOD — embed context directly in the message body using %-style
logger.info("user %d logged in", user_id)
logger.info("user %s logged in from %s", username, ip_address)
```

**Context reading:** Flag any log call kwargs other than `exc_info`. Exempt: code inside the
logging adapter/factory file itself (the file that defines `get_logger`).

---

### L23 — `logging.warn()` Deprecated

**Grep:** `\.warn\(`

**Severity:** LOW

**Auto-fixable:** Yes (→ `.warning()`)

**Applies to:** stdlib only

**Description:** `Logger.warn()` and `logging.warn()` are deprecated aliases for `.warning()`.
They emit a `DeprecationWarning` and will be removed in a future Python version. Use
`.warning()` consistently.

```python
# BAD — deprecated, emits DeprecationWarning
logger.warn("retrying connection")
logging.warn("configuration missing")

# GOOD
logger.warning("retrying connection")
logging.warning("configuration missing")
```

**Acceptable?** Never — it's always a direct rename.

---

### L24 — kwargs in Application Log Calls

**Grep:** `LOGGER_VAR_PATTERN\.\w+\([^)]*,\s*\w+=` (log call with kwargs other than `exc_info`) — collect hits for Step 2.3

**Severity:** MEDIUM

**Auto-fixable:** Yes (move values to %-style message body)

**Applies to:** all non-stdlib frameworks (structlog, loguru, custom adapters)

**Description:** kwargs in application-level log calls are an anti-pattern:

1. **Framework context is auto-injected.** Logging adapters for platforms like Temporal
   automatically propagate all platform context (workflow_id, run_id, activity_type, task_queue,
   attempt, trace_id, correlation_id, etc.) onto every log record. Application code never needs
   to pass these fields manually.

2. **Application kwargs land in a JSON blob.** In typical OTel→Grafana/ClickHouse pipelines,
   only a small fixed set of fields are promoted to top-level indexed columns. All other kwargs
   land in a `LogAttributes` JSON blob — present but not individually queryable without JSON
   extraction. They are invisible when scanning the raw log stream.

3. **%-style message body is always more discoverable.** The `Body` field is text-searchable at
   full scan speed. Context embedded in the message is immediately readable in any log viewer.

**Acceptable exceptions:**
- `exc_info=True` (attaches stack trace, not application data)
- Code inside the logging adapter/factory itself (the file that defines `get_logger`)
- stdlib projects using `extra={}` for the stdlib-specific structured fields API

```python
# BAD — context buried in JSON blob
logger.info("batch loaded", count=total, table=table_name)
logger.warning("connection failed", host=host, port=port, attempt=n, exc_info=True)

# GOOD — embed context directly in message body using %-style
logger.info("loaded %d records from %s", total, table_name)
logger.warning("connection to %s:%d failed on attempt %d", host, port, n, exc_info=True)
```

**Context reading:** Skip `exc_info=True/False`. Skip files that define the logging factory
(e.g. the file containing `get_logger` or the logger adapter class). Flag all other kwargs.

---

## Severity Classification

| ID | Name | Severity | Framework | Rationale |
|----|------|----------|-----------|-----------|
| L1 | f-string in log message | HIGH | all | Destroys structured indexing; pervasive impact |
| L2 | Inconsistent logger factory | HIGH | all | Mixed formats break log correlation at scale |
| L3 | `extra={}` in wrong framework | MEDIUM | structlog/loguru | Silently nests data; invisible to aggregation queries |
| L4 | Missing `exc_info=True` in except | HIGH | all | Makes root-cause analysis impossible |
| L5 | `print()` in production | MEDIUM | all | Bypasses observability; not critical but noisy |
| L6 | INFO inside tight loop | MEDIUM | all | Log volume explosion; performance impact |
| L7 | `logger.critical()` | MEDIUM | all | Confuses alerting; no operational value over ERROR |
| L8 | Unguarded expensive DEBUG | LOW | all | Performance waste only when DEBUG enabled |
| L9 | Warn-then-raise duplication | MEDIUM | all | Inflated error counts in dashboards |
| L10 | Credential value in log | CRITICAL | all | Security vulnerability; data exposure |
| L11 | String concatenation in log | MEDIUM | all | Same impact as L1 |
| L12 | %-style in non-stdlib logger | MEDIUM (structlog) / HIGH (loguru) | structlog/loguru | structlog: literal %s; loguru: args silently dropped |
| L13 | `extra={}` reserved key collision | CRITICAL | stdlib | Crashes caller; not caught by handleError() |
| L14 | Arbitrary kwargs in stdlib logger | CRITICAL | stdlib | TypeError crashes caller; common after migration from structlog/loguru |
| L15 | `event=` kwarg overwrites message | HIGH | structlog | Silently replaces log message with domain value |
| L16 | `dictConfig` with `disable_existing_loggers` default | HIGH | stdlib | Silently disables all pre-existing loggers |
| L17 | `basicConfig()` no-op after first call | HIGH | stdlib | Configuration silently ignored; wrong config in effect |
| L18 | `logger.exception()` outside except | MEDIUM | all | Logs `NoneType: None`; misleads debugging |
| L19 | `bind()` return value discarded | HIGH | structlog/loguru | Context silently not attached to any log call |
| L20 | `propagate=False` without handlers | HIGH | stdlib | Messages silently dropped at DEBUG/INFO |
| L21 | `logger.remove()` kills all sinks | HIGH | loguru | All log output silently stops |
| L22 | loguru kwargs silently ignored or consumed | MEDIUM | loguru | kwargs silently ignored; correct fix is %-style message body |
| L23 | `logging.warn()` deprecated | LOW | stdlib | DeprecationWarning; will be removed in future Python |
| L24 | kwargs in application log calls | MEDIUM | non-stdlib | Framework context auto-injected; kwargs land in unindexed JSON blob; use %-style |

---

## Legitimacy — Acceptable Patterns

Not every grep match is a real finding. These patterns look like violations but are not:

| Pattern | Looks like | Why acceptable |
|---------|-----------|----------------|
| `logger.info("token refreshed", token_name=name)` | L10 | Logs the name, not the value |
| `logger.debug("processing %s", item_id)` | L12 | Only flag if framework is not stdlib |
| `logger.exception("failed")` (no `exc_info=True`) | L4 | `.exception()` implies `exc_info=True` |
| `extra={"count": n}` in stdlib project | L3 | Correct stdlib pattern |
| `logger.info("...")` in a loop of known size ≤ 10 | L6 | Loop is bounded and small |
| `print(result)` in `if __name__ == "__main__":` | L5 | CLI output, not logging |
| `print(result)` in test files | L5 | Test output, acceptable |
| `extra={"user_name": val}` (non-reserved key) | L13 | Key is not in the 22 forbidden LogRecord attributes |
| `logger.info("msg", exc_info=True)` | L14, L22, L24 | `exc_info` is the one allowed kwarg — attaches stack trace, not application data |
| `logger.info("event", event="startup")` where event is intentionally the message | L15 | Intentional — must be commented explaining the choice |
| `dictConfig({..., "disable_existing_loggers": False, ...})` | L16 | Explicitly opts out of the dangerous default |
| Single `basicConfig()` in `if __name__ == "__main__":` | L17 | Controlled entrypoint, no competing calls |
| `logger.exception("msg")` inside an `except` block | L18 | Correct use — has exception context |
| `bound = logger.bind(request_id="abc")` (return captured) | L19 | Return value captured; context properly attached |
| `logger.remove(handler_id)` (specific ID) | L21 | Removes only that sink; default may still be present |
| `logger.info("user %d logged in", user_id)` | L22, L24 | Correct pattern — context in message body via %-style, no kwargs |
| Code inside the logging adapter/factory file (defines `get_logger`) | L22, L24 | Exempt — this is the framework setup, not application code |

---

## Fix Templates

### FT-L1 — f-string → %-style message body

```python
# Before
logger.info(f"loaded {count} records from {table}")

# After — embed context in message body using %-style (all frameworks)
logger.info("loaded %d records from %s", count, table)

# Also good — literal string when values are known at call site
logger.info("extraction complete")
```

### FT-L2 — Import swap to canonical factory

```python
# Before (if canonical is structlog)
import logging
logger = logging.getLogger(__name__)

# After
import structlog
logger = structlog.get_logger()
```

### FT-L3 — `extra={}` ↔ kwargs (manual — framework-dependent)

```python
# Before (structlog — extra not indexed)
logger.info("batch complete", extra={"count": n})

# After (structlog)
logger.info("batch complete", count=n)
```

### FT-L4 — Add `exc_info=True`

```python
# Before
except ConnectionError:
    logger.warning("connection failed")

# After
except ConnectionError:
    logger.warning("connection failed", exc_info=True)
```

### FT-L5 — `print()` → logger

```python
# Before
print(f"Processing {len(records)} records")

# After — embed context in message body
logger.info("processing batch of %d records", len(records))
```

### FT-L6 — INFO → DEBUG in loop

```python
# Before
for asset in assets:
    logger.info("processing asset", name=asset.name)

# After — DEBUG per item (%-style), INFO for summary
for asset in assets:
    logger.debug("processing asset %s", asset.name)
logger.info("batch of %d assets processed", len(assets))
```

### FT-L7 — `critical()` → `error()`

```python
# Before
logger.critical("database unreachable")

# After
logger.error("database unreachable", exc_info=True)
```

### FT-L8 — Guard expensive DEBUG args

```python
# Before
logger.debug("payload", data=json.dumps(large_obj))

# After
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("payload", data=json.dumps(large_obj))
```

### FT-L9 — Remove log before raise

```python
# Before
except Exception:
    logger.error("operation failed", exc_info=True)
    raise

# After (simple re-raise — let caller log)
except Exception:
    raise

# After (adding context — wrap and raise)
except Exception as e:
    raise OperationError(f"failed during {phase}") from e
```

### FT-L10 — Remove credential value

```python
# Before
logger.debug("auth configured", token=api_token)

# After
logger.debug("auth configured", credential_name=credential_name, token_present=bool(api_token))
```

### FT-L11 — Concatenation → %-style message body

```python
# Before
logger.info("loaded " + str(count) + " records")

# After — %-style (all frameworks)
logger.info("loaded %d records", count)
logger.info("loaded %d records from %s", count, table)
```

### FT-L12 — %-style in structlog/loguru (check call site, not just project)

```python
# Before (loguru direct import — arg silently ignored even if a bridge adapter exists)
from loguru import logger
logger.info("loaded %s records", count)

# After — use CANONICAL_FACTORY so the %-bridge is applied
from myproject.observability import get_logger
logger = get_logger(__name__)
logger.info("loaded %s records", count)   # adapter pre-formats before handing to loguru

# Before (vanilla loguru, no bridge — arg silently ignored)
logger.info("loaded %s records", count)

# After (vanilla loguru, no bridge)
logger.info("loaded {} records", count)   # loguru uses str.format() style

# Before (structlog — % appears literally)
logger.info("loaded %s records", count)

# After (structlog)
logger.info("loaded {} records", count)
```

**Note:** A %-style bridge in the adapter only helps calls that go **through** that adapter.
Direct `from loguru import logger` calls bypass the bridge entirely — the fix is to use
`CANONICAL_FACTORY` (e.g. `get_logger(__name__)`), not just to note that a bridge exists.

### FT-L13 — Rename reserved extra key

```python
# Before — crashes with KeyError
logger.info("action", extra={"name": user.name})

# After — prefix to avoid collision
logger.info("action", extra={"user_name": user.name})
```

### FT-L14 — Wrap arbitrary kwargs in extra={} (stdlib)

```python
# Before — crashes with TypeError in stdlib
logger.info("user login", user_id="alice", request_id="abc")

# After — wrap in extra={}
logger.info("user login", extra={"user_id": "alice", "request_id": "abc"})
```

### FT-L15 — Rename `event` kwarg in structlog

```python
# Before — silently replaces log message
logger.info("processing", event=domain_event)

# After — use a non-colliding key
logger.info("processing", domain_event=domain_event)
logger.info("processing", event_type=domain_event.type, event_id=domain_event.id)
```

### FT-L16 — Add `disable_existing_loggers: False` to dictConfig

```python
# Before
logging.config.dictConfig({
    "version": 1,
    "handlers": {...},
    "root": {...},
})

# After
logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,   # <-- add this
    "handlers": {...},
    "root": {...},
})
```

### FT-L17 — Consolidate basicConfig calls (manual)

```python
# Before — second call is silently ignored
logging.basicConfig(level=logging.WARNING)  # in library
logging.basicConfig(level=logging.DEBUG)    # in app — no-op

# After (Python 3.8+) — force override
logging.basicConfig(level=logging.DEBUG, force=True)
# or switch to dictConfig with disable_existing_loggers=False
```

### FT-L18 — Replace exception() with error() outside except block

```python
# Before — outside except, logs "NoneType: None"
logger.exception("validation failed")

# After — use error() when no exception context
logger.error("validation failed")
```

### FT-L19 — Capture bind() return value

```python
# Before — return value discarded, context not attached
logger.bind(request_id="abc")
logger.info("handling request")

# After — capture return value
logger = logger.bind(request_id="abc")
logger.info("handling request")

# Or (loguru) — use contextualize for scoped binding
with logger.contextualize(request_id="abc"):
    logger.info("handling request")
```

### FT-L20 — Add handler before propagate=False (manual)

```python
# Before — messages silently dropped
child = logging.getLogger("app.worker")
child.propagate = False

# After — add handler first
child = logging.getLogger("app.worker")
child.addHandler(logging.StreamHandler())
child.propagate = False
```

### FT-L21 — Use specific sink ID with logger.remove()

```python
# Before — removes ALL sinks including default stderr
logger.remove()
logger.add("app.log")

# After — add new sink first, then remove default by ID
logger.add("app.log")
logger.remove(0)   # 0 is loguru's default stderr sink ID
```

### FT-L22 — kwargs in loguru → %-style message body

```python
# Before — kwarg silently ignored (no {} placeholder)
logger.info("user logged in", user_id=123)

# After — embed context in message body using %-style
logger.info("user %d logged in", user_id)
logger.info("user %s logged in from %s", username, ip_address)
```

### FT-L23 — Replace warn() with warning()

```python
# Before — deprecated
logger.warn("retrying")

# After
logger.warning("retrying")
```

### FT-L24 — kwargs in application log call → %-style message body

```python
# Before — context buried in JSON blob
logger.info("batch loaded", count=total, table=table_name)
logger.warning("connection failed", host=host, port=port, attempt=n, exc_info=True)
logger.error("auth failed", user=username, reason=str(e))

# After — embed context in message body using %-style; keep exc_info=True
logger.info("loaded %d records from %s", total, table_name)
logger.warning("connection to %s:%d failed on attempt %d", host, port, n, exc_info=True)
logger.error("auth failed for user %s: %s", username, str(e), exc_info=True)
```

---

## Linting Rules to Enable

These ruff/flake8-logging-format rules directly enforce the patterns above:

| Rule | Pattern | Notes |
|------|---------|-------|
| `G001` | `.format()` in logging call | Catches format-string anti-pattern |
| `G002` | `%` formatting in logging call | **False-positive risk in stdlib projects** — %-style is correct there |
| `G003` | `+` concatenation in logging | Maps to L11 |
| `G004` | f-string in logging | Maps to L1 (ruff natively supports this) |
| `T201` | `print()` found | Maps to L5 |
| `LOG009` | `WARN` deprecated | Use `WARNING` — maps to L23 |
| `LOG015` | `basicConfig()` called without `force=True` | Maps to L17 (if available in ruff version) |

**Enable in `pyproject.toml`:**
```toml
[tool.ruff.lint]
select = [
    # existing rules...
    "G",      # flake8-logging-format
    "T201",   # print statements
    "LOG",    # logging anti-patterns
]
# If project uses stdlib-only logging, suppress G002 to avoid false positives:
ignore = ["G002"]
```

**Suggested Makefile target:**
```makefile
lint-logging:
    uv run ruff check --select G,T201,LOG $(SRC_DIRS)
```

**Note on G002:** If the project uses stdlib logging, `%-style` is correct and G002 will produce
false positives. Add `G002` to `ignore` in that case. Step 2.5 will note this recommendation
based on the detected `LOGGER_FRAMEWORK`.
