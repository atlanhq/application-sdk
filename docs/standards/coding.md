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
    - A value returned in the shape it came in: a `TypeVar`
      (`persist_file_refs(store, data: T) -> T`).
    - A callable and its arguments, forwarded: `ParamSpec`
      (`run_in_thread(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T`).
    - `__exit__` / `__aexit__`: `type[BaseException] | None`,
      `BaseException | None`, `TracebackType | None`.
    - `**kwargs` forwarded to a typed API (pydantic's `__init_subclass__`): that
      API's own type (`Unpack[ConfigDict]`), not `object`, which it rejects.
    - New code adds no site; paying one down means running
      `check_ann401_baseline.py --update` in the same change.

- **Testing Standards**
    - Testing standards are defined in [testing.md](testing.md)

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
