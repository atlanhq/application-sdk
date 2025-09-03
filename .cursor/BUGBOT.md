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
- **Exception Handling Standards**: All exception handling must follow these principles. See detailed guidelines in [exception-handling.mdc](@.cursor/rules/exception-handling.mdc).

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

- **Testing Standards**: Testing standards are defined in [testing.mdc](@.cursor/rules/testing.mdc)
- **Test Coverage**: New code has corresponding tests (unit/integration/e2e as appropriate)
- **Edge Cases**: Error conditions and boundary cases are tested
- **Mock Strategy**: External dependencies properly mocked/isolated
- **Test Naming**: Test names clearly describe the scenario being tested

### 🔒 Security & Performance

- **Input Validation**: All external inputs validated and sanitized
- **Secrets Management**: No secrets in code; proper credential handling
- **SQL Injection**: Parameterized queries used for all SQL operations
- **Performance Standards**: All performance considerations must follow these principles. See detailed guidelines in [performance.mdc](@.cursor/rules/performance.mdc).
- **Memory Management**: Resources properly closed, large datasets processed in chunks
- **DataFrame Optimization**: Appropriate dtypes, avoid unnecessary copies, use chunked processing
- **SQL Query Efficiency**: Use LIMIT, specific columns, proper WHERE clauses, connection pooling
- **Serialization Performance**: Use orjson for large datasets, implement compression
- **Algorithm Efficiency**: Use appropriate data structures, avoid O(n²) when O(n) alternatives exist
- **Caching Strategy**: Cache expensive operations and database queries
- **Async Usage**: Use async for I/O operations, sync for CPU-bound tasks

### 📊 Observability & Logging

- **Logging Standards**: logging standards are defined in [logging.mdc](@.cursor/rules/logging.mdc)
- **Metrics**: Key operations include appropriate metrics
- **Error Context**: Error logs include sufficient context for debugging
- **Trace Information**: Critical paths include trace information

## 🚨 Automated Pattern Detection Rules

### Unit Test Requirements

- **Mandatory Test Coverage**: PRs with titles starting with "fix:" MUST include corresponding test file changes
- **Test File Naming**: Test files must follow the pattern `test_*.py` and mirror source structure

### File Naming Conventions

- **Model/Dataclass Files**: Files named `base.py` containing dataclasses or pydantic models should be renamed to `models.py` or `model.py`
- **Descriptive Naming**: File names should clearly indicate their purpose and contents
- **Consistency**: Follow established naming patterns within the repository

### Directory Structure

- **Single File Directories**: Directories containing only one file should be reorganized (e.g., `events/` folder with only one file)
- **Logical Grouping**: Related functionality should be grouped in appropriate subdirectories
- **Module Organization**: Avoid creating unnecessary directory nesting for single files

### Magic String Constants

- **Operation Strings**: Hardcoded strings like `"create"`, `"update"`, `"delete"` in business logic should be extracted to constants or enums.
- **Configuration Values**: String literals used across multiple files that are loaded from environment variables should be defined in `constants.py`.

### Method Naming

- **Verbose Names**: Method names longer than 25 characters with redundant context should be simplified
- **Clear Intent**: Method names should be concise but descriptive of their primary purpose
- **Context Redundancy**: Avoid repeating class or module context in method names

### Dead Code Detection

- **Conditional Blocks**: Flag `if False:` blocks and suggest removal
- **Unused Boolean Flags**: Detect and remove unused boolean flags and conditional logic
- **Commented Code**: Remove commented-out code blocks that are no longer needed
- **Unreachable Code**: Identify and remove code that can never be executed

### Code Duplication

- **Utility Extraction**: Repeated logic should be extracted into shared utility methods
- **Configuration Patterns**: Similar configuration setup code should be centralized

### File Management

- **Premature Creation**: Flag new empty files or files with minimal content that aren't referenced elsewhere
- **Unused Files**: Detect files added then immediately marked for removal in the same PR
- **File Purpose**: Ensure every new file serves a clear, documented purpose

### Import Organization

- **Stale Imports**: When code is moved between files, check if old imports still reference moved code
- **Import Cleanup**: Remove unused imports after code reorganization
- **Dependency Management**: Ensure imports reflect actual code dependencies

### Async Pattern Consistency

- **Workflow Context**: In workflow/temporal contexts, flag sync method calls that should be async
- **Mixed Patterns**: Ensure consistent async/await usage within the same module
- **Temporal Best Practices**: Follow Temporal SDK guidelines for async operations
