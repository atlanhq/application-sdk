# Application SDK Code Review Guidelines

## Review Mental Framework

Follow this systematic review process that mirrors how experienced developers think through code changes.

### Phase 1: Immediate Safety Assessment

**Mental Question: "Could this cause immediate harm?"**
Review for critical issues first - these take priority over everything else.

#### Security Vulnerabilities

**Always flag immediately:**

- Hardcoded secrets, passwords, API keys, or tokens anywhere in code
- SQL queries using string concatenation instead of parameters
- User input directly included in system commands without sanitization
- Missing authentication checks on protected operations
- Sensitive data in log messages or error responses
- Unsafe deserialization of user-provided data

**Python SDK specific security patterns:**

- Missing input validation in handler functions
- Direct SQL execution without parameterization in `application_sdk/clients/sql.py`
- Unencrypted credential storage in configuration
- API keys or database passwords in plaintext

**Example Educational Feedback:**
"String concatenation in SQL queries creates SQL injection vulnerabilities. In our SDK handling enterprise data, this could expose sensitive customer information. Always use parameterized queries with the SQL client's execute method, which automatically escapes user input. This follows the principle of defense in depth for data security."

#### Performance Disasters

**Always flag immediately:**

- Operations that load entire large datasets into memory unnecessarily
- N+1 query problems (database queries inside loops)
- Synchronous blocking operations in async contexts
- Missing pagination for operations returning large collections
- Expensive computations repeated unnecessarily without caching
- DataFrame operations without proper dtype optimization
- Missing LIMIT clauses on SQL queries
- String concatenation in loops (use join patterns)
- Synchronous database calls in async workflow activities

**Python SDK performance patterns:**

- Loading entire tables without chunking in metadata extraction
- Missing connection pooling in SQL clients
- Inefficient serialization using default json instead of orjson
- Not using appropriate DataFrame dtypes for memory optimization
- Processing large datasets without generators or chunking

#### Critical System Stability

**Always flag immediately:**

- Operations that could corrupt shared state or data
- Missing error handling that could crash the application
- Resource leaks (unclosed files, connections, database sessions)
- Race conditions in concurrent code
- Operations without timeouts that could hang indefinitely
- Silent exception swallowing without re-raising
- Missing finally blocks for resource cleanup
- Direct prop mutation in reactive frameworks

---

### Phase 2: Code Quality Foundation

**Mental Question: "Is this code maintainable and reliable?"**

#### Code Organization and File Structure

**Critical organization patterns:**

- **File Location Consistency**: Code must be placed in appropriate directories
  - Decorators belong in `application_sdk/decorators/`, not scattered across modules
  - Interceptors should be consolidated, not duplicated across files
  - Similar functionality must be grouped together
  - Dead code or "no need" code must be removed immediately

**Import Organization Standards:**

- Imports must be at the top of files (flag any imports inside functions unless required)
- Import order: standard library → third-party → local application
- Group imports by source and separate with blank lines
- Remove unused imports immediately
- No circular dependencies between modules

**File Naming and Directory Structure:**

- Use descriptive file names that clearly indicate purpose
- Follow SDK naming conventions: `Base` prefix, not `Generic`
- Avoid creating directories for single files
- Move shared constants to `application_sdk/constants.py`
- Files with similar responsibilities should be in the same directory

```python
# ❌ REJECT: Poor file organization
# Having decorators scattered across multiple files
application_sdk/lock/__init__.py  # Contains decorators
application_sdk/observability/decorators/  # Contains other decorators

# ✅ REQUIRE: Centralized organization
application_sdk/decorators/locks.py  # All lock decorators
application_sdk/decorators/observability_decorator.py  # All observability decorators
```

#### Python Code Organization and Style

**Critical Python patterns:**

- snake_case for variables/functions, PascalCase for classes, UPPER_SNAKE_CASE for constants
- Use type hints for all function parameters and return values
- Use dataclasses or Pydantic for structured data
- Follow PEP 8 formatting standards (120 character line length)
- Use double quotes for strings consistently
- Use list comprehensions when they improve readability

**Universal naming rules:**

- Boolean variables must start with `is_`, `has_`, `can_`, `should_`
- No single-letter variables except loop counters (`i`, `j`, `k`)
- No abbreviations that aren't widely understood
- Function names must be verbs describing what they do
- Constants must describe their purpose, not just their value
- Variables named `temp`, `test`, `debug`, `foo`, `bar` are forbidden

#### Function and Class Structure

**Universal quality rules:**

- Functions should do one thing (Single Responsibility Principle)
- Maximum function length: 75 lines
- Maximum class size: 300 lines
- No nested conditionals deeper than 3 levels
- No functions with more than 7 parameters
- Extract complex logic into well-named helper functions

**Code Consolidation Requirements:**

- **Always flag repetitive logic**: If similar code appears in multiple methods, extract it into a shared function
- **Separate class responsibilities**: Classes doing multiple unrelated things must be split (e.g., RedisClient handling both client and lock functionality)
- **Centralize shared constants**: Magic strings or numbers used across files must be moved to constants
- **Extract testable functions**: Complex logic that can't be easily unit tested must be broken into smaller, testable functions

#### Configuration and Environment Management

**Configuration consistency patterns:**

- **Environment variable naming**: Use consistent, descriptive names (e.g., `FAIL_WORKFLOW_ON_REDIS_UNAVAILABLE` not `REDIS_ERROR_MODE`)
- **Default value strategy**: Define which parameters should be programmatic vs environment-driven
- **Development/Production compatibility**: All features must work in both local development and production environments
- **Configuration validation**: All environment variables must be validated on application startup
- **Parameter inference**: Values that can be inferred (like application name, run ID) should not require explicit passing

```python
# ❌ REJECT: Poor configuration management
max_locks = os.getenv("MAX_LOCKS")  # No validation, no defaults
REDIS_ERROR = "true"  # Hardcoded, not configurable

# ✅ REQUIRE: Proper configuration management
FAIL_WORKFLOW_ON_REDIS_UNAVAILABLE = os.getenv("FAIL_WORKFLOW_ON_REDIS_UNAVAILABLE", "false").lower() == "true"
max_locks = int(os.getenv("MAX_LOCKS", "10"))  # Default with validation
```

#### Documentation Requirements

**Always require:**

- **Complete docstrings**: Public functions must have Google-style docstrings with usage examples and parameter documentation
- **Parameter documentation**: All parameters must be documented with types, defaults, and constraints
- **TODO management**: TODO/FIXME comments must include issue references and deadlines, or be removed
- **Complex algorithms**: Must have explanatory comments explaining business logic and "why"
- **Magic numbers**: Must be extracted to `application_sdk/constants.py` with explanations

**Documentation quality standards:**

```python
# ✅ DO: Complete documentation
def create_distributed_lock(
    lock_name: str,
    max_locks: int = 10,
    ttl_seconds: int = 300,
    owner_id: Optional[str] = None
) -> LockManager:
    """
    Create a distributed lock manager for coordinating resource access.

    Args:
        lock_name: Unique identifier for the lock resource
        max_locks: Maximum number of concurrent lock holders (default: 10)
        ttl_seconds: Lock expiration time in seconds (default: 300)
        owner_id: Lock owner identifier, defaults to application_name:run_id

    Returns:
        LockManager: Configured lock manager instance

    Raises:
        ValueError: If max_locks <= 0 or lock_name is empty
        ConnectionError: If unable to connect to lock store

    Example:
        >>> lock_mgr = create_distributed_lock("metadata_extraction", max_locks=5)
        >>> async with lock_mgr.acquire():
        ...     await process_metadata()
    """
```

#### Import and Dependency Organization

**Always enforce:**

- No unused imports or dependencies
- Consistent import ordering within files
- No circular dependencies between modules
- Standard library → third-party → local application imports
- Group imports by source and separate with blank lines

---

### Phase 3: Testing and Verification

**Mental Question: "How do we know this works correctly?"**

#### Test Coverage Requirements

**Always enforce:**

- New public functions must have corresponding unit tests
- New API endpoints must have integration tests
- New workflow activities must have activity tests
- Minimum test coverage: 85% for new code
- Critical business logic must have comprehensive edge case testing

**Testing command:** `uv run coverage run -m pytest --import-mode=importlib --capture=no --log-cli-level=INFO tests/ -v --full-trace --hypothesis-show-statistics`

#### Test Quality Standards

**Always require:**

- Test names must clearly describe what is being tested
- Tests must be independent (no shared state between tests)
- External dependencies must be mocked in unit tests
- Tests must cover error conditions and edge cases
- No tests calling real external services (databases, APIs, etc.)
- Use pytest fixtures for common setup/teardown
- Use hypothesis for property-based testing

**Test organization:**

- Unit tests: `tests/unit/`
- Integration tests: `tests/integration/`
- End-to-end tests: `tests/e2e/`
- Test files mirror source structure
- Test file names start with `test_`
- Each test file has one `describe` block per class/function

#### Error Handling Patterns

**Critical exception handling rules:**

- **Always re-raise exceptions** after logging unless in non-critical operations
- **Use SDK-specific exceptions**: Use `ClientError` and internal error codes from `application_sdk/common/error_codes.py`, not generic exceptions
- **Add comprehensive try-catch**: Operations that can fail must be wrapped in try-catch with specific exception types
- **Error context**: Error messages must include debugging context (operation, parameters, state)
- **Resource cleanup**: Failed operations must be logged with appropriate detail
- **Non-critical operations**: May swallow exceptions to prevent cascading failures

```python
# ✅ DO: Proper error handling with SDK exceptions
from application_sdk.common.error_codes import ClientError

try:
    result = database_connection.execute_query(query)
    return result
except ConnectionError as e:
    logger.error(f"Database connection failed for query {query[:50]}...: {e}")
    raise ClientError(f"Database connection failed: {e}")
except ValueError as e:
    logger.error(f"Invalid query parameters: {e}")
    raise ClientError(f"Query validation failed: {e}")

# ❌ DON'T: Generic exceptions without context
try:
    result = some_operation()
except Exception as e:
    logger.error(f"Error: {e}")  # No context, generic exception
```

---

### Phase 4: System Integration Review

**Mental Question: "How does this fit into the broader system?"**

#### API Design and Consistency

**Always check:**

- HTTP status codes are semantically correct and consistent
- Error response formats match established patterns
- Breaking changes are properly versioned
- Input validation happens at appropriate boundaries
- API contracts are backward compatible
- FastAPI route handlers follow established patterns

#### Data Handling Patterns

**Strongly typed models required:**

- Use Pydantic models for all data crossing boundaries
- No "naked" dictionaries/objects for structured data
- All user inputs must be validated before processing
- Data transformations must preserve type safety
- Database schema changes must be reversible
- Sensitive data must be handled according to privacy requirements

#### Performance and Scalability Patterns

**Performance optimization opportunities:**

- **Parallelization identification**: Flag operations that could benefit from parallel processing (file downloads, uploads, batch operations)
- **Async enforcement**: Ensure blocking operations don't interfere with async workflows
- **Resource limits**: Validate constraints like Redis key length limits, connection pool sizes
- **Batch processing**: Encourage batching for operations that can be grouped

**Scale-aware review questions:**

- Will this design work with enterprise-scale datasets (millions of records)?
- Are we maintaining constant memory usage regardless of input size?
- Do we have appropriate caching strategies for database queries?
- Are expensive operations batched appropriately?
- Is the database query pattern efficient at scale?

**Memory management requirements:**

- Process large datasets in chunks, not all at once
- Use appropriate DataFrame dtypes for memory efficiency
- Use connection pooling for database operations
- Close resources in finally blocks or context managers

```python
# ✅ DO: Identify parallelization opportunities
# Flag this pattern for parallelization suggestion
for file in files:
    await download_file(file)  # Sequential, could be parallel

# Suggest this improvement:
tasks = [download_file(file) for file in files]
await asyncio.gather(*tasks)  # Parallel processing
```

---

### Phase 5: Future Impact Assessment

**Mental Question: "What are the long-term consequences of this change?"**

#### Maintainability and Extensibility

**Always assess:**

- Can other team members easily understand and modify this code?
- Are we following established patterns consistent with the rest of the SDK?
- Would adding new features require major changes to this design?
- Is this creating or reducing technical debt?
- Are we building abstractions at the right level?

#### Architecture Alignment

**Always verify:**

- Does this change move toward or away from our target SDK architecture?
- Are we respecting established layer boundaries (clients, handlers, activities, workflows)?
- Is this solving the root cause or adding a workaround?
- Are dependencies flowing in the correct direction?
- Is this increasing or decreasing system complexity?

#### Team Knowledge Distribution

**Always consider:**

- Is this creating knowledge silos or sharing knowledge?
- Are patterns and practices documented for team learning?
- Can new team members contribute to this area effectively?
- Are we using technologies and patterns the team can support long-term?

---

## Universal Anti-Patterns - Always Reject

### Debug and Development Code

**Never allow in production:**

- `print()`, `console.log`, `debugger` statements
- Commented-out code blocks (suggest removal)
- TODO/FIXME comments without issue references or deadlines
- Variables named `temp`, `test`, `debug`, `foo`, `bar`
- Development-only configuration in production files
- `if False:` blocks and unused boolean flags

### Resource Management Anti-Patterns

**Always flag:**

- File handles not properly closed (missing try-with-resources/context managers)
- Database connections not returned to connection pool
- Missing timeout configurations for external service calls
- Memory leaks from circular references or event listeners
- Expensive resources created repeatedly instead of reused

### Data Safety Anti-Patterns

**Always flag:**

- Naked dictionaries/objects for structured data (require Pydantic models)
- Missing null/None checks before accessing properties
- Type coercion without explicit validation
- Mutable global state that could cause race conditions
- Direct manipulation of shared data structures without synchronization

### Python-Specific Anti-Patterns

**Always flag:**

- **Mutable default arguments**: Using mutable objects (dict, list) as default parameter values
- **Mixed type annotations**: Inconsistent typing approaches in function signatures
- **Blocking operations in async code**: Using `time.sleep()` instead of `await asyncio.sleep()`
- **Unreachable code blocks**: `if False:` blocks, conditions that can never be True
- **Invalid range operations**: Operations like `random.randint(0, max_value-1)` where `max_value` could be 0

```python
# ❌ REJECT: Mutable default arguments
def bad_function(items: list = []):  # Shared mutable default!
    items.append("new_item")
    return items

def bad_config(settings: dict = {}):  # Shared mutable default!
    settings["new_key"] = "value"
    return settings

# ✅ REQUIRE: Immutable defaults with None
def good_function(items: Optional[list] = None) -> list:
    if items is None:
        items = []
    items.append("new_item")
    return items

def good_config(settings: Optional[dict] = None) -> dict:
    if settings is None:
        settings = {}
    settings["new_key"] = "value"
    return settings

# ❌ REJECT: Mixed typing approaches
def bad_typing_mix(
    param1: str,
    param2,  # No type hint
    param3: "SomeClass",  # String type hint
    param4: List[dict]  # Correct typing
):
    pass

# ✅ REQUIRE: Consistent type annotations
from typing import List, Optional
from some_module import SomeClass

def good_typing_consistency(
    param1: str,
    param2: int,  # Proper type hint
    param3: SomeClass,  # Direct import, not string
    param4: List[dict]
) -> dict:
    pass

# ❌ REJECT: Blocking in async code
async def bad_async_function():
    await some_io_operation()
    time.sleep(5)  # Blocks entire event loop!
    await another_operation()

# ✅ REQUIRE: Proper async patterns
async def good_async_function():
    await some_io_operation()
    await asyncio.sleep(5)  # Non-blocking async sleep
    await another_operation()

# ❌ REJECT: Unreachable code and invalid ranges
def bad_validation(max_value: int):
    if False:  # Unreachable code
        print("This never executes")

    # Can crash if max_value is 0
    random_slot = random.randint(0, max_value - 1)  # ValueError if max_value <= 0
    return random_slot

# ✅ REQUIRE: Proper validation and reachable code
def good_validation(max_value: int) -> int:
    if max_value <= 0:
        raise ValueError(f"max_value must be positive, got: {max_value}")

    random_slot = random.randint(0, max_value - 1)
    return random_slot
```

### Performance Anti-Patterns

**Always flag:**

- String concatenation in loops (use join patterns)
- Repeated expensive calculations without memoization
- Complex operations in render/template loops
- Missing indexes on frequently queried database columns
- Synchronous operations blocking async workflows
- Loading entire DataFrames when chunked processing should be used

### Code Organization Anti-Patterns

**Always reject:**

- **Scattered functionality**: Related code spread across multiple inappropriate locations
- **Import organization violations**: Imports not at the top, wrong order, or unused imports
- **File misplacement**: Decorators outside `decorators/`, constants not in `constants.py`
- **Dead code**: Any code marked as "no need" or unused
- **DRY violations**: Repeated logic that should be extracted into shared functions
- **Naming inconsistencies**: Using different naming patterns for similar concepts

---

## Repository-Specific Customization Zones

### Zone 1: Python SDK Advanced Patterns

**Advanced Python patterns:**

- Use context managers for resource handling (`with` statements)
- Implement proper `__str__` and `__repr__` methods for classes
- Use generators for memory-efficient iteration
- Follow asyncio best practices for concurrent code
- Use dataclasses or Pydantic models for structured data
- Implement proper exception hierarchies in `error_codes.py`

**Temporal workflow patterns:**

- Activities must be async when calling external services
- Workflow definitions must be deterministic
- Use proper activity retry policies
- Handle workflow failures gracefully
- Use Temporal's built-in serialization

### Zone 2: Enterprise SDK Business Rules

**Data processing compliance:**

- All metadata extraction must be reversible and traceable
- Query processing must preserve data lineage information
- All data transformations must maintain audit trails
- Customer data must be handled according to privacy policies
- Database schema changes must be backward compatible

**Performance requirements:**

- Metadata extraction must handle enterprise-scale databases (millions of objects)
- Query processing must support concurrent execution
- Memory usage must remain constant regardless of dataset size
- Database operations must use connection pooling

### Zone 3: Integration and Infrastructure Rules

**Temporal integration patterns:**

- All workflow activities must have proper timeout and retry policies
- Service dependencies must be declared explicitly in activity definitions
- Circuit breakers required for external service dependencies
- Workflow state must be serializable and deterministic

**Database integration patterns:**

- All SQL clients must use parameterized queries
- Connection pooling required for all database operations
- Transaction boundaries must be explicit
- Database migrations must be reversible

### Zone 4: Team and Process Specific Rules

**Code review process:**

- Performance-sensitive changes require load testing
- Database schema changes require DBA review
- Breaking API changes require architecture review
- Security-sensitive changes require security team review

**Documentation requirements:**

- Architecture decisions must be documented in conceptual docs
- Public APIs must have comprehensive docstrings with examples
- Breaking changes must update corresponding documentation
- Module changes must update concept documentation mapping

### Zone 5: Quality and Compliance Standards

**Quality standards:**

- Code complexity metrics must be below established thresholds
- Technical debt must be tracked and addressed
- Performance benchmarks must be maintained
- Test coverage must be above 85% for new code

**Observability requirements:**

- All operations must include appropriate metrics
- Error conditions must be logged with context
- Trace information required for critical paths
- Use `AtlanLoggerAdapter` for all logging with proper context

---

## Educational Approach - Always Explain Why

When flagging any issue, always provide educational context:

### Include These Elements:

1. **Specific Issue**: Exactly what pattern or problem was detected
2. **Impact**: Why this matters for maintainability, performance, security, or team productivity
3. **Better Approach**: Specific alternative that follows established patterns
4. **Principle**: Which coding principle or architectural pattern this relates to
5. **Context**: How this relates to enterprise SDK scale and requirements

### Example Educational Feedback:

Instead of: "Don't use string concatenation in loops"
Provide: "String concatenation in loops creates O(n²) complexity because strings are immutable - each concatenation creates a new string object. In our enterprise SDK processing millions of metadata records, this could cause significant performance degradation and memory issues. Use join() patterns instead, which maintain O(n) complexity. This follows the principle of choosing appropriate data structures for the access pattern and becomes critical when handling large-scale enterprise datasets."

### Focus on Growth and Learning:

- Help developers understand the reasoning behind patterns
- Connect specific rules to broader architectural principles
- Explain how choices affect future maintainability and team productivity
- Build intuition for making good decisions independently
- Reference Python best practices and async programming patterns
- Connect to performance implications at enterprise scale
- Relate to data integrity and security considerations
