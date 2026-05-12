# Error Recovery Patterns Reference

Catalogue of Python silent-error patterns used by the signal-over-noise skill (Phase 1: Surface).
Covers how to detect each pattern, classify it, and fix it. Eleven patterns (P1–P11).

---

## Pattern Catalogue

Eleven patterns to search for. Each entry lists the grep pattern, what it means, and when it is
a genuine bug vs an acceptable idiom.

---

### P1 — Bare `except: pass`

**Grep:** `except\s*:\s*pass`
**Severity:** CRITICAL
**What it is:** Catches *every* exception including `KeyboardInterrupt`, `SystemExit`,
`GeneratorExit`, and discards it silently. The hardest class of bugs to debug.

```python
# BAD
try:
    connect()
except:
    pass

# GOOD
try:
    connect()
except Exception:
    logger.warning("Failed to connect", exc_info=True)
```

**Acceptable?** Never. Even cleanup paths should log at DEBUG.

---

### P2 — `except SomeException: pass` (typed but silent)

**Grep:** `except\s+\w.*:\s*pass`
**Severity:** HIGH
**What it is:** Typed catch that still discards silently. Slightly better than P1 (at least it
won't swallow `SystemExit`) but still loses the stack trace entirely.

```python
# BAD
try:
    parse_config()
except ValueError:
    pass

# GOOD
try:
    parse_config()
except ValueError:
    logger.warning("Config parse failed, using defaults", exc_info=True)
```

**Acceptable?** Only for truly trivial "best-effort" operations where failure is 100% expected
AND the surrounding code handles the missing result (e.g. optional cache warm-up). Must have a
comment explaining the reasoning.

---

### P3 — `contextlib.suppress(Exception)` (broad)

**Grep:** `contextlib\.suppress\(`
**Severity:** HIGH when broad (`Exception`, `BaseException`); LOW when narrow (`FileNotFoundError`)
**What it is:** Syntactic sugar for `except X: pass`. The problem is it suppresses *silently*.

```python
# BAD — no visibility into what was suppressed
with contextlib.suppress(Exception):
    cache.invalidate(key)

# GOOD — narrow + log
try:
    cache.invalidate(key)
except CacheError:
    logger.debug("Cache invalidation skipped", exc_info=True)
```

**Acceptable?** `suppress(FileNotFoundError)` on a `os.remove()` cleanup path is fine.
`suppress(Exception)` almost never is.

---

### P4 — Overly broad `except Exception` / `except BaseException` (without logging)

**Grep:** `except\s+(Exception|BaseException)\s*:`
**Severity:** HIGH (no logging) / MEDIUM (logged but missing `exc_info`)
**What it is:** Catches everything but the specific type is unknown. Even when logged, the
exception type is often omitted.

```python
# BAD — too broad, no traceback
except Exception as e:
    logger.warning(f"Something failed: {e}")

# GOOD — log with full traceback
except Exception:
    logger.warning("Unexpected error during fetch", exc_info=True)
    raise   # or handle specifically
```

**Acceptable?** At top-level handlers (worker main loops, HTTP request handlers) as a catch-all
*if* properly logged with `exc_info=True`. Not acceptable anywhere else.

---

### P5 — `except` block logs without `exc_info=True`

**Grep:** `except.*:` paired with `logger\.(warning|error|critical|exception)` in the block
but missing `exc_info=True`
**Severity:** MEDIUM
**What it is:** The message is logged, but the stack trace is discarded. When debugging at
3 AM, you get the message but not where the error originated.

```python
# BAD — message logged but traceback lost
except ValueError as e:
    logger.warning(f"Validation failed: {e}")

# GOOD — full traceback preserved
except ValueError:
    logger.warning("Validation failed", exc_info=True)
```

**Special case:** `logger.exception(...)` automatically includes `exc_info=True` — no fix needed.

**Acceptable?** Only when the exception is 100% expected and carries no diagnostic value
(e.g. `except StopIteration` in a manual iterator — but prefer `for` loops instead).

---

### P6 — Bare `except:` (no type, no pass — but still bad)

**Grep:** `^\s*except\s*:`
**Severity:** CRITICAL
**What it is:** Like P1 but may have a body. Still catches `KeyboardInterrupt`, `SystemExit`, etc.

```python
# BAD
except:
    logger.warning("Error")   # still catches SystemExit!

# GOOD
except Exception:
    logger.warning("Error", exc_info=True)
```

**Acceptable?** Never. Always specify at least `Exception`.

---

### P7 — `except` block that only returns a value (error-to-return-value)

**Grep:** `except.*:` followed by `return` (with no log call in between)
**Severity:** HIGH
**What it is:** Exception is converted to a return value (None, {}, [], False) with no trace.
Callers see a wrong result and have no idea why.

```python
# BAD
try:
    return parse(data)
except Exception:
    return {}

# GOOD
try:
    return parse(data)
except Exception:
    logger.warning("Parse failed, returning empty", exc_info=True)
    return {}
```

**Acceptable?** Rarely. Consider raising a domain-specific exception instead so callers can
decide whether to handle or propagate.

---

### P8 — `except ImportError` without logging

**Grep:** `except\s+ImportError`
**Severity:** LOW
**What it is:** Optional-dependency guard. Usually intentional — but if the module is expected
to be present, silent suppression hides environment problems.

```python
# ACCEPTABLE — truly optional dependency
try:
    import uvloop
    uvloop.install()
except ImportError:
    pass   # uvloop is optional; asyncio default is fine

# BAD — expected dependency silently missing
try:
    import pandas
except ImportError:
    pass   # will fail later with confusing AttributeError
```

**Acceptable?** Yes, when the import is genuinely optional AND the fallback path is correct.
Add a comment. Log at DEBUG if the module is preferred but not required.

---

### P9 — `except` block that only assigns a variable (no logging)

**Grep:** `except.*:` followed only by an assignment (`\w+ = `)
**Severity:** HIGH
**What it is:** Exception sets a flag or default value with no trace. Combines P7's
error-hiding with no logging.

```python
# BAD
try:
    result = fetch()
except Exception:
    result = None

# GOOD
try:
    result = fetch()
except Exception:
    logger.warning("Fetch failed, proceeding with None", exc_info=True)
    result = None
```

---

### P10 — `asyncio.gather` with `return_exceptions=True` (unexamined)

**Grep:** `return_exceptions=True`
**Severity:** MEDIUM
**What it is:** Exceptions from coroutines are returned as values in the results list instead
of being raised. If the results list is not inspected for `Exception` instances, errors vanish.

```python
# BAD — exceptions silently discarded
results = await asyncio.gather(*tasks, return_exceptions=True)
process(results)  # results may contain Exception objects

# GOOD — check for exceptions
results = await asyncio.gather(*tasks, return_exceptions=True)
for r in results:
    if isinstance(r, Exception):
        logger.warning("Task failed: %s", r, exc_info=r)
```

**Acceptable?** `return_exceptions=True` is fine; the pattern only becomes a bug when the
results are not subsequently checked for exception instances.

---

### P11 — `logging.Filter` Exceptions Propagate to Caller (CRASH)

**Grep:** `class\s+\w+.*logging\.Filter` or `def filter\(self` inside a class that inherits `logging.Filter`
**Severity:** HIGH
**What it is:** Custom `logging.Filter.filter()` methods that raise an unhandled exception crash
the logging call site — unlike handler errors, filter exceptions are NOT caught by `handleError()`.
The `Logger.handle()` method calls `self.filter(record)` without any try/except.

This means a buggy filter (one that fails on an unexpected record format, a missing attribute,
or a network call that times out) will propagate the exception directly to whatever code called
`logger.info()` (or similar), crashing the caller.

```python
# BAD — if SomeExternalService raises, so does logger.info()
class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = SomeExternalService.get_current_request_id()  # can raise!
        return True

# GOOD — wrap filter body in try/except with a safe fallback
class RequestContextFilter(logging.Filter):
    def filter(self, record):
        try:
            record.request_id = SomeExternalService.get_current_request_id()
        except Exception:
            record.request_id = "unknown"
        return True

# BAD — attribute access on record that might not exist
class EnvFilter(logging.Filter):
    def filter(self, record):
        if record.environment == "production":  # AttributeError if not set!
            return True
        return False

# GOOD — use getattr with a default
class EnvFilter(logging.Filter):
    def filter(self, record):
        if getattr(record, "environment", None) == "production":
            return True
        return False
```

**Acceptable?** Never — filter methods must never let exceptions propagate.

---

## Severity Classification

| Severity | Criteria |
|----------|----------|
| **CRITICAL** | Exception silently discarded with zero logging; catches `BaseException`/bare `except`; will actively hide crashes |
| **HIGH** | Exception logged without `exc_info` (stack trace lost); error converted to return value silently; broad catch in non-top-level code; filter that can crash caller |
| **MEDIUM** | Broad catch that is properly logged; `asyncio.gather` results unchecked; `contextlib.suppress` with narrow type but no logging |
| **LOW** | Optional-import guard with comment; broad catch at documented top-level handler; suppress on truly expected transient errors |

---

## Legitimacy — Acceptable Patterns

Not every match is a bug. Classify as **acceptable** when ALL conditions hold:

| Pattern | Acceptable when |
|---------|----------------|
| `except ImportError: pass` | Module is genuinely optional; fallback path is correct; comment explains |
| `except SomeError: pass` | 100% expected (e.g. `StopIteration`); comment explains; no diagnostic value lost |
| `contextlib.suppress(FileNotFoundError)` | File absence is a normal non-error condition (cleanup, cache miss) |
| Broad catch at top level | Worker main loop or HTTP request handler with `exc_info=True` and re-raise or graceful shutdown |
| `except X: cleanup(); raise` | Reraises after cleanup — exception is not swallowed |

When a finding is acceptable, note the justification and skip it from the remediation plan.

---

## Fix Templates

Mechanical fixes per category. Apply exactly — don't over-engineer.

### FT-1: Add logging to silent-swallow

```python
# Before
except SomeError:
    pass

# After
except SomeError:
    logger.warning("<context: what was being attempted>", exc_info=True)
```

### FT-2: Add `exc_info=True` to existing log

```python
# Before
except SomeError as e:
    logger.warning(f"Failed: {e}")

# After
except SomeError:
    logger.warning("Failed: <static description>", exc_info=True)
```

### FT-3: Add logging before error-to-return-value

```python
# Before
except Exception:
    return {}

# After
except Exception:
    logger.warning("<context>", exc_info=True)
    return {}
```

### FT-4: Add exception inspection to `asyncio.gather` results

```python
# After gather call, add:
for _r in _results:
    if isinstance(_r, Exception):
        logger.warning("Async task failed", exc_info=_r)
```

### FT-5: Replace broad `contextlib.suppress(Exception)` with logged try/except

```python
# Before
with contextlib.suppress(Exception):
    do_thing()

# After
try:
    do_thing()
except Exception:
    logger.warning("<context>", exc_info=True)
```

### FT-6: Narrow overly-broad catch (manual — requires knowing the right exception types)

```python
# Before
except Exception:
    ...

# After — use the specific exception the called code raises
except (SpecificError, OtherError):
    ...
```

Leave as `# TODO(signal-over-noise): [P4] narrow this catch` when auto-fixing.

### FT-7: Wrap logging.Filter body in try/except

```python
# Before — filter exception propagates to logging call site, crashing caller
class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = get_current_request_id()  # can raise!
        return True

# After — always provide a safe fallback
class RequestContextFilter(logging.Filter):
    def filter(self, record):
        try:
            record.request_id = get_current_request_id()
        except Exception:
            record.request_id = "unknown"
        return True

# Before — attribute access without default
class EnvFilter(logging.Filter):
    def filter(self, record):
        return record.environment == "production"  # AttributeError if not set!

# After — use getattr with a default
class EnvFilter(logging.Filter):
    def filter(self, record):
        return getattr(record, "environment", None) == "production"
```

---

## Linting Rules to Enable

Add to `pyproject.toml` `[tool.ruff.lint]` `select` list to prevent regressions:

| Rule | Description | Category |
|------|-------------|----------|
| `BLE001` | Blind exception caught | Broad catch |
| `S110` | `try`-`except`-`pass` detected | Silent swallow |
| `S112` | `try`-`except`-`continue` detected | Silent swallow in loop |
| `TRY400` | Use `logging.exception` instead of `logging.error` | Missing exc_info |
| `TRY401` | Redundant `exc_info` in `logging.exception` | Cleanup |
| `TRY300` | Consider moving `return` out of `try` | Error-hiding return |
| `TRY302` | Useless `try`-`except` (just reraises) | Dead code |

Minimal addition to `pyproject.toml`:

```toml
[tool.ruff.lint]
select = [
  # ... existing rules ...
  "BLE",    # blind exceptions
  "S110",   # try-except-pass
  "S112",   # try-except-continue
  "TRY400", "TRY401",  # logging with exc_info
]
```

Note: `TRY300` and `TRY302` can be noisy on existing codebases — introduce separately after
fixing the higher-severity issues.
