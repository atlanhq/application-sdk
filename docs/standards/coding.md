# Development Standards

This document outlines the development standards for the Application SDK, including code formatting, testing, and logging guidelines.

## Code Formatting

- **Python Style Guide**
    - Follow PEP 8 style guide
    - Use type hints for all function parameters and return values
    - Maximum line length: 120 characters
    - Use double quotes for strings
    - Use 4 spaces for indentation
    - Use snake_case for variable and function names
    - Use PascalCase for class names
    - Use UPPER_CASE for constants

- **Documentation**
    - All functions, classes, and modules must have docstrings
    - Use Google-style docstrings
    - Include type information in docstrings
    - Document all public APIs
    - Build docs using `uv sync --all-extras --all-groups && uv run poe generate-apidocs`
    - Docs must go under ./docs

- **Single Responsibility**
    - Each function should do exactly one thing
    - Functions should be small and focused
    - If a function needs a comment to explain what it does, it should be split
    - In `application_sdk/`, a function must stay under ruff's C901 (McCabe
      complexity 10) and PLR0915 (50 statements). The functions already over
      either limit are named in a baseline that only shrinks
      (see [ci.md](ci.md)); a new or grown one fails pre-commit. Split it.

- **DRY (Don't Repeat Yourself)**
    - Extract repeated code into reusable functions
    - Share common logic through proper abstraction
    - Maintain single sources of truth

- **Clean Structure**
    - Keep related code together
    - Organize code in a logical hierarchy
    - Use consistent file and folder naming conventions

- **Encapsulation**
    - Hide implementation details
    - Expose clear interfaces
    - Move nested conditionals into well-named functions

- **Code Quality Maintenance**
    - Refactor continuously
    - Fix technical debt early
    - Leave code cleaner than you found it

- **No `Any` in a signature**
    - `typing.Any` in a parameter or return annotation turns type checking off
      exactly where a consumer types against the SDK. ruff's ANN401 flags it,
      held to a per-file baseline that only shrinks
      (see [ci.md](ci.md)).
    - A value the function only passes on, stores or inspects with
      `isinstance`: `object`.
    - A value returned as the same class it came in: a `TypeVar`, but only for
      the cases the code really preserves. `persist_file_refs` rebuilds a
      changed model with `model_copy` (same class) but a changed container as
      the built-in, so it is overloaded: `data: M -> M` for a `BaseModel`,
      `object -> object` otherwise.
    - A callable and its arguments, forwarded: `ParamSpec`
      (`run_in_thread(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T`).
    - `__exit__` / `__aexit__`: `type[BaseException] | None`,
      `BaseException | None`, `TracebackType | None`.
    - `**kwargs` forwarded to a typed API (pydantic's `__init_subclass__`): that
      API's own type (`Unpack[ConfigDict]`), not `object`, which it rejects.
    - A file's count may not rise; paying one down means running
      `check_ann401_baseline.py --update` in the same change. The count is per
      file, so do not trade one `Any` for another in the same file.

- **Time and Clocks**
    - A timestamp is timezone-aware UTC: `datetime.now(UTC)`,
      `datetime.fromtimestamp(ts, UTC)`. A naive reading takes the host's zone;
      pods run in UTC, so it only goes wrong on SDR hosts and laptops, where
      nobody is looking. Ruff `DTZ` enforces this.
    - Parsing: `strptime(s, fmt).replace(tzinfo=UTC)` only when `fmt` has no
      `%z` and the string is known to be UTC (`replace` relabels, it does not
      convert). When the string carries an offset, parse it with `%z` and
      convert with `.astimezone(UTC)`; replacing the tzinfo there would shift
      the instant by the offset.
    - A duration is two `time.monotonic()` readings, never two `time.time()`
      readings: the wall clock can step between them (NTP, VM resume) and the
      difference comes out negative or hours long.
      `tests/unit/test_no_wall_clock_elapsed.py` fails on
      `time.time() - <name>` under `application_sdk/`.
    - Inside `run()`, use `application_sdk.app.now()` instead; both of the
      above are non-deterministic there and the workflow sandbox refuses them.

- **Testing Standards**
    - Testing standards are defined in [testing.md](testing.md)

- **Imports**
    - Import at the top of the module. In `application_sdk/` an import inside a function needs a reason a reader can check: an optional dependency, a module absent on an api-only install, a Temporal sandbox constraint, or a measured import-time cost. "Circular" is not one: break the cycle instead, by moving the shared piece into a module both sides can import or by passing the dependency in at bootstrap (as observability receives its object-store sink).
    - `tests/unit/test_import_cycles.py` builds the import graph from source, in-function imports included, and fails on any cycle not closed by an edge in its `_DEFERRED_CYCLE_EDGES`. That list holds the in-function imports that still close a cycle, each with its reason; a cycle through one of them is not reported, so the list only shrinks: an entry that no longer exists or no longer closes a cycle fails the test, and a module-level import is never excused by it. It also holds a ceiling on the number of in-function imports, which only goes down.
    - A package `__init__` that re-exports heavy siblings serves them lazily through `application_sdk._install.lazy_getattr` (see `app`, `execution`, `storage`, `infrastructure`), so importing one leaf of the package does not import all of it.
    - The `Import Isolation` job in `sdk-gate.yaml` imports every module of `application_sdk` in a fresh interpreter, so a module that only imports after another one has loaded fails CI.

- **Logging Standards**
    - Logging standards are defined in [logging.md](logging.md)

- **Exception Handling**
    - Exception handling standards are defined in [exceptions.md](exceptions.md)

- **Performance Standards**
    - Performance standards are defined in [performance.md](performance.md)

## Security, Performance, and Resource Management

- **Security Considerations**
    - Test for common vulnerabilities (e.g., injection flaws, insecure configurations).
    - Verify input validation and sanitization logic rigorously.
    - Ensure proper authentication and authorization are enforced.
    - Incorporate automated dependency vulnerability scanning into the CI/CD pipeline.
    - Avoid logging sensitive information; refer to [logging.md](logging.md).

- **Performance Optimization**
    - Establish baseline performance metrics for critical components.
    - Include load tests for expected production scenarios.
    - Benchmark and optimize resource-intensive operations.
    - Identify and address performance regressions proactively.

- **Resource Management & Leak Prevention**
    - Ensure resources (files, network connections, database sessions) are properly closed/released.
    - Design tests to verify resource cleanup, especially for long-running processes or components managing external resources.
    - Be mindful of potential memory leaks in complex data structures or long-lived objects.
