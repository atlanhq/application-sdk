# v3 Code Quality Review Rules

Enforced by pre-commit hooks (ruff, isort, pyright) and v3 coding conventions.

---

## Import Rules

### Top-Level Imports (Critical)

All imports MUST be at the module top level. Inline imports inside functions are a violation.

**Exceptions (must be documented with a comment):**
- Heavy optional dependencies that are slow to import (e.g., `duckdb`, `pandas`, `daft`) — comment: `# lazy import: heavy dependency`
- Circular import avoidance — comment: `# deferred import: circular dependency`
- Optional feature imports guarded by `try/except ImportError`

**Flag these patterns:**
```python
# BAD — import inside function
def process_data(self):
    from application_sdk.storage import upload_file  # violation
    ...

# BAD — import inside method
async def run(self, input: MyInput) -> MyOutput:
    import json  # violation — stdlib imports always at top
    ...

# OK — lazy import with justification
def analyze(self):
    import duckdb  # lazy import: heavy dependency, only needed in this code path
    ...
```

### Import Ordering (isort, black profile)

Order: stdlib -> third-party -> local. Separated by blank lines. Enforced by isort with `profile = "black"`.

**Flag:**
- Mixed stdlib and third-party imports in same block
- Local imports before third-party
- Missing blank line between import groups

### Star Imports

`from module import *` is forbidden everywhere except `__init__.py` re-exports.

### Unused Imports

Flag any imported name not used in the file. Exception: re-exports in `__init__.py` with `__all__`.

---

## Logging Rules (Ruff G001, G003, G004, T201)

### No f-strings in Log Calls (Critical — G004)

```python
# BAD
logger.info(f"Processing {count} records")
logger.error(f"Failed: {error}")

# GOOD
logger.info("Processing %d records", count)
logger.error("Failed: %s", error)
```

### No String Concatenation in Log Calls (G003)

```python
# BAD
logger.info("Processing " + str(count) + " records")

# GOOD
logger.info("Processing %d records", count)
```

### No % Formatting Inline in Log Calls (G001)

```python
# BAD
logger.info("Processing %d records" % count)

# GOOD — let the logger handle it
logger.info("Processing %d records", count)
```

### No print() Statements (T201)

```python
# BAD
print("Debug output")
print(f"Result: {result}")

# GOOD
logger.debug("Debug output")
logger.debug("Result: %s", result)
```

### Structured Context over String Interpolation

**Two orthogonal rules — do not conflate them:**

1. **Message argument MUST use %-style** (enforced by ruff G004 — never f-strings)
2. **Additional structured context kwargs are VALID and ENCOURAGED** for Loki/Grafana indexing

Both can appear in the same call:

```python
# BAD — f-string in message (G004 violation)
logger.info(f"Fetched {count} records from {source}")

# GOOD — %-style message + structured kwargs (BOTH are correct simultaneously)
logger.info(
    "Fetched %d records",
    count,
    source=source,
    duration_seconds=duration,
)

# ALSO GOOD — %-style only, no kwargs
logger.info("Fetched %d records from %s in %.2fs", count, source, duration)

# ALSO GOOD — static message + kwargs only
logger.info(
    "Fetched records",
    record_count=count,
    source=source,
    duration_seconds=duration,
)
```

The SDK's `AtlanLoggerAdapter` wraps loguru which supports structured keyword args. When reviewing, flag f-strings in log messages (G004) but do NOT flag structured kwargs — they are valid and improve observability.

---

## Naming Conventions

- **Classes**: PascalCase (`MyConnectorApp`, `FetchInput`, `ExtractOutput`)
- **Functions/methods**: snake_case (`fetch_data`, `process_records`)
- **Constants**: UPPER_SNAKE_CASE (`MAX_RETRIES`, `DEFAULT_TIMEOUT`)
- **Private**: single underscore prefix (`_internal_method`, `_helper`)
- **Module-private directories**: underscore prefix (`_temporal/`, `_dapr/`)
- **Input/Output classes**: named after the task they serve (`FetchInput`/`FetchOutput` for `fetch()` task)
- **App classes**: descriptive, suffixed with meaningful context (`SqlMetadataExtractor`, not `MyApp`)

---

## Function and Method Quality

### Size
Flag methods exceeding ~50 lines. Suggest extraction of helper methods.

### Complexity
Flag deeply nested code (3+ levels of nesting). Suggest early returns, guard clauses.

### Single Responsibility
Flag methods that do multiple unrelated things (e.g., fetch + transform + upload in one method without task boundaries).

### Return Types
All public methods must have return type annotations. Exception: test methods.

### Docstrings
- Public API methods (on `App`, `Handler`, public functions) should have docstrings
- Internal methods don't require docstrings if the name is self-explanatory
- Don't flag missing docstrings on tests or private helpers

---

## Error Handling

### No Bare Except

```python
# BAD
try:
    ...
except:
    pass

# BAD
try:
    ...
except Exception:
    pass  # swallowed

# GOOD
try:
    ...
except SpecificError as e:
    logger.warning("Recoverable: %s", e, exc_info=True)
```

### No Silent Swallowing

Every `except` block must either:
1. Re-raise the exception
2. Log with `exc_info=True` at WARNING or above
3. Wrap and raise a new exception with the original as `__cause__`

### Exception Chaining

```python
# BAD — loses original traceback
except ConnectionError as e:
    raise AppError("Connection failed")

# GOOD — preserves chain
except ConnectionError as e:
    raise AppError("Connection failed") from e
```

---

## Async Patterns

### No Sync in Async

Flag synchronous blocking calls inside `async def` methods:
- `time.sleep()` -> `asyncio.sleep()`
- `open().read()` for large files -> `aiofiles` or `run_in_thread`
- `requests.get()` -> `httpx` async client

### Task Context Required

Inside `@task` methods, use `self.task_context` for:
- `run_in_thread()` for blocking ops
- `heartbeat()` for progress
- `get_heartbeat_details()` for resume

---

## Pre-Commit Compliance

The following must pass before merge:
1. **ruff** — lint + format
2. **ruff-format** — code formatting
3. **isort** — import sorting (profile: black)
4. **pyright** — type checking (standard mode)
5. **check-merge-conflict** — no conflict markers
6. **debug-statements** — no `breakpoint()`, `pdb` imports
7. **trailing-whitespace** — no trailing whitespace
8. **conventional-pre-commit** — commit messages follow conventional format

Flag any code that would clearly fail these checks.

---

## Deprecation Patterns

When v2 APIs are modified or removed:
- Deprecation shim must exist with `warnings.warn()` at import time
- Warning message must include the v3 replacement path
- Scheduled removal version must be documented (v3.1.0)

```python
# Real example: application_sdk/test_utils/integration/__init__.py
import warnings
warnings.warn(
    "application_sdk.test_utils.integration is deprecated and will be removed "
    "in v3.1.0. Use application_sdk.testing.integration instead.",
    DeprecationWarning,
    stacklevel=2,
)
from application_sdk.testing.integration import *  # noqa: E402,F401,F403
```
