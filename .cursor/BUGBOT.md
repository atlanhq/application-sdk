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

#### Import and Dependency Organization

**Always enforce:**

- No unused imports or dependencies
- Consistent import ordering within files
- No circular dependencies between modules
- Standard library → third-party → local application imports
- Group imports by source and separate with blank lines

#### Function and Class Structure

**Universal quality rules:**

- Functions should do one thing (Single Responsibility Principle)
- Maximum function length: 75 lines
- Maximum class size: 300 lines
- No nested conditionals deeper than 3 levels
- No functions with more than 7 parameters
- Extract complex logic into well-named helper functions

#### Documentation Requirements

**Always require:**

- Public functions must have Google-style docstrings explaining purpose and parameters
- Complex algorithms must have explanatory comments
- Magic numbers must be extracted to `application_sdk/constants.py` with explanations
- Regular expressions must have comments explaining what they match
- Non-obvious business logic must be documented with context
- All functions must have type hints for parameters and return values

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

- Always re-raise exceptions after logging unless in non-critical operations
- Use specific exception types instead of generic `Exception`
- Error messages must include debugging context
- Failed operations must be logged with appropriate detail
- Retryable vs non-retryable errors must be distinguished
- Error responses must not leak sensitive information
- Custom exceptions must be defined in `application_sdk/common/error_codes.py`

```python
# ✅ DO: Proper exception handling
try:
    result = database_connection.execute_query(query)
    return result
except ConnectionError as e:
    logger.error(f"Database connection failed for query {query[:50]}...: {e}")
    raise  # Re-raise to propagate the error
except ValueError as e:
    logger.error(f"Invalid query parameters: {e}")
    raise

# ❌ DON'T: Swallowing exceptions
try:
    result = some_operation()
except Exception as e:
    logger.error(f"Error: {e}")
    # Missing raise - this swallows the exception!
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

### Performance Anti-Patterns

**Always flag:**

- String concatenation in loops (use join patterns)
- Repeated expensive calculations without memoization
- Complex operations in render/template loops
- Missing indexes on frequently queried database columns
- Synchronous operations blocking async workflows
- Loading entire DataFrames when chunked processing should be used

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
- Public APIs must have comprehensive docstrings
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
