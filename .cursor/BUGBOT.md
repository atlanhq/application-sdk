# Project Review Guidelines - Application SDK

## Core Principles

**Less Code is Better** - Prioritize simplicity, readability, and maintainability over cleverness. Every line of code is a liability that must be justified.

## Critical Review Checklist

### 🔍 Code Quality & Minimalism

- **Necessity Check**: Is this code absolutely necessary? Can existing functionality be reused?
- **Single Responsibility**: Does each function/class do exactly one thing well?
- **DRY Violations**: Any repeated logic that should be extracted into shared utilities?
- **Cognitive Load**: Can a new developer understand this change with less cognitive load?
- **Magic Numbers/Strings**: All constants properly defined and named in @application_sdk/constants.py
- **Spell Check**: No typos in code, comments, docstrings, or documentation
- **Exception Handling Standards**: All exception handling must follow these principles. See detailed guidelines in [exception-handling.mdc](mdc:.cursor/rules/exception-handling.mdc).

### 🏗️ Architecture & Design

- **Module Boundaries**: Changes respect existing module responsibilities
- **Error Handling**: Proper exception handling with meaningful error messages
- **Resource Management**: Files, connections, and resources properly closed/released
- **Async Patterns**: Proper async/await usage where applicable
- **Configuration**: No hardcoded values; use proper configuration management

### 📝 Documentation Requirements

- **Docstrings**: All public functions, classes, and modules have Google-style docstrings
- **Type Hints**: All function parameters and return values are typed
- **Complex Logic**: Non-obvious business logic has inline comments explaining "why"

### 🧪 Testing

- **Testing Standards**: Testing standards are defined in [testing.mdc](mdc:.cursor/rules/testing.mdc)
- **Test Coverage**: New code has corresponding tests (unit/integration/e2e as appropriate)
- **Edge Cases**: Error conditions and boundary cases are tested
- **Mock Strategy**: External dependencies properly mocked/isolated
- **Test Naming**: Test names clearly describe the scenario being tested

### 🔒 Security & Performance

- **Input Validation**: All external inputs validated and sanitized
- **Secrets Management**: No secrets in code; proper credential handling
- **SQL Injection**: Parameterized queries used for all SQL operations
- **Performance Standards**: All performance considerations must follow these principles. See detailed guidelines in [performance.mdc](mdc:.cursor/rules/performance.mdc).
- **Memory Management**: Resources properly closed, large datasets processed in chunks
- **DataFrame Optimization**: Appropriate dtypes, avoid unnecessary copies, use chunked processing
- **SQL Query Efficiency**: Use LIMIT, specific columns, proper WHERE clauses, connection pooling
- **Serialization Performance**: Use orjson for large datasets, implement compression
- **Algorithm Efficiency**: Use appropriate data structures, avoid O(n²) when O(n) alternatives exist
- **Caching Strategy**: Cache expensive operations and database queries
- **Async Usage**: Use async for I/O operations, sync for CPU-bound tasks

### 📊 Observability & Logging

- **Logging Standards**: logging standards are defined in [logging.mdc](mdc:.cursor/rules/logging.mdc)
- **Metrics**: Key operations include appropriate metrics
- **Error Context**: Error logs include sufficient context for debugging
- **Trace Information**: Critical paths include trace information
