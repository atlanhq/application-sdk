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

Order: stdlib → third-party → local. Separated by blank lines. Enforced by isort with `profile = "black"`.

**Flag:**
- Mixed stdlib and third-party imports in same block
- Local imports before third-party
- Missing blank line between import groups

### Star Imports

`from module import *` is forbidden everywhere except `__init__.py` re-exports.

### Unused Imports

Flag any imported name not used in the file. Exception: re-exports in `__init__.py` with `__all__`.

---

## Suppression Comments

### Suppression comments require an inline justification (Important)

`# noqa`, `# type: ignore`, `# pylint: disable`, `# mypy: ignore`,
`# pragma: no cover` all bypass a quality gate. A bare suppression
silently disables the check; an inline justification ("# noqa: PLC0415
— lazy import: heavy dependency") preserves the decision history and
the next reader can validate it.

**Flag any new suppression comment that:**
- Lacks a same-line rationale after the directive: bare `# noqa`,
  `# noqa: E501` (no reason), `# type: ignore` (no reason).
- Disables a category broadly when the actual issue is local:
  blanket `# noqa` instead of `# noqa: <specific-code>`.
- Adds `# pragma: no cover` on production code paths (acceptable
  for `if TYPE_CHECKING:` blocks or `__main__` guards; suspicious
  anywhere else).

**Required form:**

```python
# Acceptable
import duckdb  # noqa: PLC0415 — lazy import: heavy dependency
result: Any  # type: ignore[no-any-return] — upstream library returns dict

# Reject
import duckdb  # noqa
result: Any  # type: ignore
```

**Severity:** Important. The suppression itself isn't a bug, but
silent suppressions accumulate and degrade the value of the
underlying gate.

---

## Configuration and Constants

### Tunables and identifiers belong in `constants.py` (Important)

User-visible metric names, log/trace attribute prefixes, service
names, and numeric tunables (sample intervals, batch sizes, retry
backoffs, timeouts) MUST live in `application_sdk/constants.py`
sourced from `ATLAN_`-prefixed env vars with documented defaults.
Inline values can't be overridden by operators and tend to drift
when copies appear in multiple call sites.

**Flag any new code that:**
- Hard-codes a string used as a metric/trace/log attribute name or
  prefix (`"application.custom"`, service names, namespace prefixes)
  in a module other than `constants.py`.
- Hard-codes a numeric tunable (timeout, interval, batch size,
  retry/backoff parameter) in a module other than `constants.py`.
- Defines the same default value inline at the use site that
  already exists in `constants.py` — pick one; it should be the
  `constants.py` import.

**Expected pattern:** the constant is defined once in `constants.py`
with `os.getenv("ATLAN_<NAME>", "<default>")` and imported at every
use site.

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

### Log exceptions with `exc_info`, never via formatted strings (Important)

The structured way to capture an exception traceback in a log call
is `logger.error(msg, exc_info=True)` or `logger.exception(msg)`.
Doing this manually — formatting `str(exc)` / `repr(exc)` /
`traceback.format_exc()` into the message string — defeats Loki/
Grafana's structured indexing, loses the structured `exception.*`
attributes, and forces operators to grep raw text rather than query
fields.

**Flag any new log call that:**
- Builds a string that includes `str(exc)`, `repr(exc)`,
  `f"{exc}"`, or any pattern that drops the traceback into the
  message body, instead of passing the exception via `exc_info`.
- Calls `traceback.format_exc()` and concatenates the result into a
  log message.
- Logs an exception inside an `except` block without `exc_info=True`
  or using `logger.exception(...)`.

**Expected pattern:**

```python
try:
    do_work()
except SomeError as exc:
    logger.error("operation failed", exc_info=True)  # or:
    logger.exception("operation failed")  # implies exc_info=True
```

**Severity:** Important (QUAL + DX). Mis-logged exceptions are
debugger-quality regressions that compound over the codebase.

### Structured Context over String Interpolation

```python
# BAD — context buried in string
logger.info("Fetched %d records from %s in %.2fs", count, source, duration)

# GOOD — structured kwargs for Loki/Grafana indexing
logger.info(
    "Fetched records",
    record_count=count,
    source=source,
    duration_seconds=duration,
)
```

Note: The SDK uses both patterns. When reviewing, flag cases where structured kwargs would clearly improve observability (high-cardinality values, values you'd want to filter/aggregate on).

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

## Control Flow

### Exhaust enum variants when matching state (Important)

When matching on an `Enum`, status string, or other discriminator
with a known fixed set of values, the branch chain must either
handle every variant explicitly or use a default branch that
records the unhandled value (typically a `WARNING` log plus an
`unknown` bucket on the relevant metric). Silent under-coverage
shows up as dashboards that mislabel real transitions and metrics
that under-count.

**Flag any new code that:**
- Has an `if x == A elif x == B` chain over a known `Enum` /
  status set without exhausting the variants or providing a default
  branch.
- Uses `match` / `case` against an `Enum` without a `case _:`.
- Introduces a metric or log attribute labelled by an enum value
  but only emits a subset of the enum's variants.

**Detection heuristic:** when a new branch over a `Status`-shaped
value appears, find the enum/source-of-truth declaration and compare
the branches against the variants. Any missing variant without an
explicit default is a finding.

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

### Exception chains must preserve the cause with `from exc` (Critical)

When raising a new exception from inside an `except` block, the
`raise NewError(...) from exc` form preserves the original exception
in `__cause__`. Dropping `from exc` produces a `During handling of
the above exception, another exception occurred` traceback that
hides the original cause and breaks our error-classification
pipeline (the wire-format `cause` field is empty). Pre-commit
doesn't reliably catch this — the reviewer must.

**Flag any new code that:**
- Has a `raise X(...)` inside an `except Y as exc:` block without
  `from exc` (or `from None` if intentionally suppressing).
- Re-raises after wrapping: `raise NewError("...")` where the
  outer `try/except` clearly catches the cause.
- Uses `raise X(...) from None` without an inline comment
  justifying the suppression (rare; usually a bug).

**Detection heuristic:** for every `raise <ExceptionName>(` added on
a line inside an `except ... as <name>:` block (i.e. the diff shows
a `raise` indented under an `except`), the same statement must
include `from <name>` (the captured exception, or `None` if
documented).

**Use the categorical leaf class** that matches the failure; pass
`cause=e` (the dataclass field) and also chain with `from e` so the
traceback is preserved:

```python
from application_sdk.errors import DependencyUnavailableError, PreconditionError

# BAD — loses traceback, plain base class
except ConnectionError as e:
    raise AppError("Connection failed")

# BAD — preserves chain but uses unhelpful base class
except ValueError as exc:
    raise PreconditionError("operation precondition failed")

# GOOD — typed leaf, cause preserved
except ConnectionError as e:
    raise DependencyUnavailableError(
        message="Connection failed",
        service="database", cause=e,
    ) from e

# GOOD — preserves the cause
except ValueError as exc:
    raise PreconditionError("operation precondition failed") from exc
```

**Severity:** Critical. A broken exception chain destroys
debuggability and silently zeros out the error-taxonomy `cause`
field consumers depend on.

---

## Abstraction Boundaries

### Don't wrap upstream APIs in single-purpose helpers (Minor)

Thin wrappers around upstream APIs that only rename without adding
type safety, validation, defaults, retries, or any other behaviour
are net negative — they add a maintenance burden, hide the upstream
documentation, and force every consumer to learn two names for the
same thing.

**Flag any new helper module that:**
- Re-exports an upstream API surface (OpenTelemetry, httpx,
  temporalio, pydantic, etc.) with one-line wrapper functions whose
  body is essentially the upstream call.
- Adds a `def record_*` / `def emit_*` / `def get_*` that only
  calls the upstream client with no other logic.

Prefer exposing the upstream object directly and letting callers
use the native API. Add a wrapper only when it provides typed
arguments, default labels, retries, deadlines, validation, or other
real value beyond renaming.

**Severity:** Minor. Suggest removal; do not block.

---

## Async Patterns

### No Sync in Async

Flag synchronous blocking calls inside `async def` methods:
- `time.sleep()` → `asyncio.sleep()`
- `open().read()` for large files → `aiofiles` or `run_in_thread`
- `requests.get()` → `httpx` async client

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
