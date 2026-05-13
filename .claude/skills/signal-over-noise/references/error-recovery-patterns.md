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

### P12 — Untyped builtin raise where a typed `AppError` code applies

**Grep:** `raise\s+(ValueError|RuntimeError|Exception|TypeError|NotImplementedError|OSError|KeyError|LookupError)\b`
**Severity:** HIGH (CRITICAL when inside an `@task`-decorated activity body or `@activity.defn`-decorated function)
**What it is:** SDK code raises a bare Python builtin. The Automation Engine
receives an opaque string — no `category`, no `code`, no `audience`, no
`retryable`. Dashboards are blind; on-call routing is impossible. Direct
expression of the BLDX-1261 audit scope.

```python
# BAD — unattributable on the wire
raise ValueError("Engine is not initialized. Call load() first.")

# GOOD — typed, attributable
from application_sdk.errors import InternalError
raise InternalError(
    message="Engine is not initialized. Call load() first.",
    component="sql_client",
    invariant="load_before_use",
)
```

**Acceptable?** Only inside dataclass `__post_init__` / stdlib validator methods
where Python semantics require `TypeError` / `ValueError` for stdlib
interoperability (e.g. Pydantic validators, `__init__` argument checks at the
Python level). Must have a comment explaining why the builtin is required.

**Fix:** FT-8. Use `typed-error-prescription.md` §4 (cookbook) or §3 (litmus
tests) to select the leaf.

---

### P13 — Legacy `AtlanError` subclass raise (deprecated stack)

**Grep:** `raise\s+(ClientError|ApiError|OrchestratorError|WorkflowError|IOError|CommonError|DocGenError|ActivityError|AtlanError)\b`
**Severity:** HIGH (deprecated; scheduled for removal in v4.0 per
`application_sdk/common/error_codes.py:51-63`)
**What it is:** `AtlanError` and its subclasses emit a `DeprecationWarning` at
construction time and reach AE as opaque strings. They produce no typed wire
envelope.

```python
# BAD
raise IOError("Object store download failed")

# GOOD (standalone raise — no enclosing exception to chain)
from application_sdk.errors import DependencyUnavailableError
raise DependencyUnavailableError(
    message="Object store download failed",
    service="object_store",
)

# GOOD (inside except block — chain the cause)
except SomeError as exc:
    raise DependencyUnavailableError(
        message="Object store download failed",
        service="object_store",
        cause=exc,
    ) from exc
```

**Acceptable?** Never, except `IOError` hits — confirm the name refers to the `AtlanError`
subclass (imported from `application_sdk.common.error_codes`) rather than the Python builtin
alias for `OSError`.

**Fix:** FT-9. If the raise site uses a legacy constant, look it up in the §5 migration table
in `typed-error-prescription.md`. If there is no constant (bare `raise AtlanError(msg)`), use
the §3 litmus tests to select a leaf directly.

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

**Typed-error requirement.** Whenever a fix surfaces or re-raises an exception,
the replacement **must** use a typed `AppError` subclass from
`application_sdk.errors`. See
[`typed-error-prescription.md`](typed-error-prescription.md) for the full
catalogue (14 SDK leaves), litmus tests, SDK-context cookbook (§4), the
exhaustive legacy `AtlanError` → `AppError` migration table (§5), and the
mandatory `cause`/`from` rules (§6). **Never** propose raising bare
`Exception` / `ValueError` / `RuntimeError`; **never** propose raising legacy
`AtlanError` subclasses (`ClientError`, `IOError`, `CommonError`, etc.).
Use the §8 surface-or-swallow decision tree to choose between FT-1a and FT-1b.

### FT-1a: Add logging to silent-swallow (log-and-continue — best-effort paths)

Use when the §8 decision tree confirms "best-effort / caller does not depend
on success". Log at WARNING if the failure is unexpected; DEBUG if it is
fully anticipated.

```python
# Before
except SomeError:
    pass

# After (log-and-continue)
except SomeError:
    logger.warning("<context: what was being attempted>", exc_info=True)
```

### FT-1b: Log and re-raise as typed `AppError` (surfacing paths)

Use when the §8 decision tree says the caller depends on the operation or this
is inside an activity body. Pick the leaf from `typed-error-prescription.md` §4.

```python
# Before
except SomeError:
    pass

# After (log + typed re-raise)
except SomeError as exc:
    logger.warning("<context: what was being attempted>", exc_info=True)
    raise <TypedLeaf>(
        message="<context: what was being attempted>",
        cause=exc,
        # evidence fields from leaves.py
    ) from exc
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

### FT-3a: Add logging before error-to-return-value (log-and-continue — caller treats default as valid)

Use when the caller genuinely treats the fallback return value as a valid
outcome. Log at WARNING.

```python
# Before
except Exception:
    return {}

# After (log-and-continue)
except Exception:
    logger.warning("<context>", exc_info=True)
    return {}
```

### FT-3b: Convert error-to-return-value to typed re-raise (dominant fix)

The default fix — use unless FT-3a's condition is clearly met. Convert the
swallow into a typed raise so callers see a real failure rather than a silently
wrong result.

```python
# Before
except Exception:
    return {}

# After (typed re-raise — caller must handle or propagate)
except Exception as exc:
    raise <TypedLeaf>(
        message="<context: what was being attempted>",
        cause=exc,
    ) from exc
```

### FT-4: Add exception inspection to `asyncio.gather` results

```python
# After gather call, add:
for _r in _results:
    if isinstance(_r, Exception):
        logger.warning("Async task failed", exc_info=_r)
```

### FT-5a: Replace broad `contextlib.suppress(Exception)` with logged try/except (best-effort)

Use when §8 confirms best-effort / caller does not depend on success.

```python
# Before
with contextlib.suppress(Exception):
    do_thing()

# After (log-and-continue)
try:
    do_thing()
except Exception:
    logger.warning("<context>", exc_info=True)
```

### FT-5b: Replace broad `contextlib.suppress(Exception)` with typed re-raise (surfacing)

Use when the call is not genuinely best-effort.

```python
# Before
with contextlib.suppress(Exception):
    do_thing()

# After (typed re-raise)
try:
    do_thing()
except Exception as exc:
    raise <TypedLeaf>(
        message="<context: what failed>",
        cause=exc,
    ) from exc
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

### FT-8: Convert untyped builtin raise to typed `AppError` (manual — leaf choice needs context)

Look up the right leaf in `typed-error-prescription.md` §4 (SDK-context cookbook)
or §3 (litmus tests). Preserve the original message verbatim.

```python
# Before
raise ValueError("aws_role_arn is required")

# After
from application_sdk.errors import InvalidInputError
raise InvalidInputError(
    message="aws_role_arn is required",
    field="aws_role_arn",
)
```

```python
# Before
raise RuntimeError("Engine is not initialized. Call load() first.")

# After
from application_sdk.errors import InternalError
raise InternalError(
    message="Engine is not initialized. Call load() first.",
    component="sql_client",
    invariant="load_before_use",
)
```

When wrapping an upstream exception, always use both `cause=exc` **and**
`raise ... from exc` (see `typed-error-prescription.md` §6).

### FT-9: Convert legacy `AtlanError` raise to typed `AppError` (manual — use migration table)

Look up the legacy constant in `typed-error-prescription.md` §5. The table
is exhaustive and covers every constant in
`application_sdk/common/error_codes.py`.

```python
# Before
raise ClientError("Engine not initialized. Call load() first.")

# After
from application_sdk.errors import InternalError
raise InternalError(
    message="Engine not initialized. Call load() first.",
    component="sql_client",
    invariant="load_before_use",
)
```

```python
# Before
raise IOError("Object store download failed")

# After (standalone — no enclosing exception)
from application_sdk.errors import DependencyUnavailableError
raise DependencyUnavailableError(
    message="Object store download failed",
    service="object_store",
)

# After (inside except block — chain the cause)
except SomeError as exc:
    raise DependencyUnavailableError(
        message="Object store download failed",
        service="object_store",
        cause=exc,
    ) from exc
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

To prevent regressions on P12 (untyped builtin raises inside `application_sdk/`),
a CI-level AST check is more practical than a ruff rule (false-positive rate is high
without a per-file allowlist). A grep-based approach can't correctly exclude raises
inside `__post_init__` or validator bodies because those raises appear on a different
line from the function definition. Use this AST-based check instead:

```bash
python - <<'PY'
import ast, pathlib, sys

BUILTIN_RAISES = {
    "ValueError", "RuntimeError", "Exception", "TypeError",
    "NotImplementedError", "OSError", "KeyError", "LookupError",
}
# Raises inside these methods / decorator names are acceptable stdlib-interop
INTEROP_METHOD_NAMES = {"__post_init__", "__init__"}
INTEROP_DECORATOR_NAMES = {"field_validator", "validator"}

class Checker(ast.NodeVisitor):
    def __init__(self, path: pathlib.Path):
        self._path = path
        self._interop_depth = 0
        self.findings: list[str] = []

    def _is_interop(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        if node.name in INTEROP_METHOD_NAMES:
            return True
        for dec in node.decorator_list:
            name = (dec.id if isinstance(dec, ast.Name)
                    else dec.func.id if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)
                    else None)
            if name in INTEROP_DECORATOR_NAMES:
                return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        in_interop = self._is_interop(node)
        if in_interop:
            self._interop_depth += 1
        self.generic_visit(node)
        if in_interop:
            self._interop_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Raise(self, node: ast.Raise) -> None:
        if self._interop_depth > 0 or node.exc is None:
            return
        exc = node.exc
        name = (exc.id if isinstance(exc, ast.Name)
                else exc.func.id if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name)
                else None)
        if name in BUILTIN_RAISES:
            self.findings.append(f"{self._path}:{node.lineno}: raise {name}")

findings: list[str] = []
for path in pathlib.Path("application_sdk").rglob("*.py"):
    try:
        checker = Checker(path)
        checker.visit(ast.parse(path.read_text(), filename=str(path)))
        findings.extend(checker.findings)
    except SyntaxError:
        pass

if findings:
    print("\n".join(findings), file=sys.stderr)
    sys.exit(1)
PY
```

This is tracked under BLDX-1261; enforcement is out of scope for this skill.
P13 (legacy `AtlanError`) will disappear once BLDX-1261 lands and can be
enforced by deleting `application_sdk/common/error_codes.py` in v4.0.
