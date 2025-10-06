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

#### Dapr Component Configuration

**Always flag immediately:**

- Critical Dapr components (e.g., state stores, secret stores) configured with `ignoreErrors: true`. These components should fail fast on misconfiguration to prevent silent runtime failures. Exceptions can be made for components explicitly documented as safe to use with `ignoreErrors: true` in production environments.

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

---

## Educational Approach - Always Explain Why

When flagging any issue, always provide educational context:

### Include These Elements:

1. **Specific Issue**: Exactly what pattern or problem was detected
2. **Impact**: Why this matters for maintainability, performance, security, or team productivity
3. **Better Approach**: Specific alternative that follows established patterns
4. **Principle**: Which coding principle or architectural pattern this relates to
5. **Context**: How this relates to enterprise SDK scale and requirements

---