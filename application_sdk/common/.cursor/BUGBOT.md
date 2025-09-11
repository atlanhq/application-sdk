# Common Code Review Guidelines - Shared Utilities and Constants

## Context-Specific Patterns

This directory contains shared utilities, constants, error codes, and common functionality used across the SDK. Code here must be high-quality, well-tested, and designed for reuse.

### Phase 1: Critical Common Code Safety Issues

**Constants Management:**

- **All magic strings and numbers must be moved to constants.py**: No hardcoded values scattered across the codebase
- **Centralized configuration**: Related constants should be grouped together with clear naming
- **Environment variable patterns**: Use consistent naming conventions for environment variables
- **Shared constant keys**: Constants used by multiple modules (like configuration keys) must be defined here

**Error Code Standardization:**

- **Use internal SDK error codes**: All custom exceptions should be defined in `error_codes.py`
- **Specific exception types**: No generic `Exception` or `ValueError` for SDK-specific errors
- **Error hierarchies**: Related errors should inherit from common base exceptions
- **Consistent error messages**: Similar errors should have consistent message formats

```python
# ✅ DO: Proper constants and error management
# In constants.py
DISTRIBUTED_LOCK_CONFIG_KEY = "distributed_lock_config"
DEFAULT_LOCK_TTL_SECONDS = 300
DEFAULT_MAX_LOCKS = 10
REDIS_KEY_PREFIX = "application_sdk"

# Environment variable naming conventions
FAIL_WORKFLOW_ON_REDIS_UNAVAILABLE = os.getenv("FAIL_WORKFLOW_ON_REDIS_UNAVAILABLE", "false").lower() == "true"
DATABASE_TIMEOUT_SECONDS = int(os.getenv("DATABASE_TIMEOUT_SECONDS", "30"))

# In error_codes.py
class SDKError(Exception):
    """Base exception for all SDK errors."""

class ClientError(SDKError):
    """Errors related to client operations."""

class LockAcquisitionError(SDKError):
    """Errors related to distributed lock operations."""

# ❌ REJECT: Scattered constants and generic errors
# Found across multiple files:
LOCK_TTL = 300  # In one file
DEFAULT_TIMEOUT = 300  # In another file
"distributed_lock"  # Hardcoded string in various places

# Using generic exceptions:
raise Exception("Lock failed")  # Should be LockAcquisitionError
raise ValueError("Invalid config")  # Should be ConfigurationError
```

### Phase 2: Utility Architecture Patterns

**Utility Function Design:**

- **Single responsibility**: Each utility function should do exactly one thing
- **Pure functions**: Utilities should avoid side effects where possible
- **Type safety**: All utility functions must have comprehensive type hints
- **Error handling**: Utilities must handle edge cases gracefully
- **Documentation**: Complete docstrings with usage examples

**Code Reuse and DRY Principles:**

- **Extract repeated logic**: Common patterns across modules should become utility functions
- **Consolidate similar utilities**: Functions with overlapping purposes should be unified
- **Shared abstractions**: Common interface patterns should be abstracted into base classes
- **Configuration utilities**: Common configuration patterns should be centralized

```python
# ✅ DO: Proper utility function design
def validate_environment_variable(
    var_name: str,
    default_value: str,
    valid_values: Optional[List[str]] = None,
    value_type: type = str
) -> Any:
    """
    Validate and convert environment variable with comprehensive error handling.

    Args:
        var_name: Name of environment variable
        default_value: Fallback value if not set
        valid_values: List of allowed values (optional)
        value_type: Expected type for conversion

    Returns:
        Validated and converted value

    Raises:
        ConfigurationError: If value is invalid or conversion fails

    Example:
        >>> timeout = validate_environment_variable(
        ...     "DB_TIMEOUT", "30", value_type=int
        ... )
        >>> mode = validate_environment_variable(
        ...     "LOG_LEVEL", "INFO", valid_values=["DEBUG", "INFO", "WARNING", "ERROR"]
        ... )
    """
    raw_value = os.getenv(var_name, default_value)

    try:
        # Type conversion
        if value_type == bool:
            converted_value = raw_value.lower() in ('true', '1', 'yes', 'on')
        elif value_type == int:
            converted_value = int(raw_value)
        elif value_type == float:
            converted_value = float(raw_value)
        else:
            converted_value = raw_value

        # Validation
        if valid_values and converted_value not in valid_values:
            raise ConfigurationError(
                f"Invalid value for {var_name}: {raw_value}. "
                f"Valid values: {valid_values}"
            )

        return converted_value

    except (ValueError, TypeError) as e:
        raise ConfigurationError(
            f"Failed to convert {var_name}={raw_value} to {value_type.__name__}: {e}"
        )

# ❌ REJECT: Poor utility design
def bad_get_config(name):  # No type hints, no validation, no documentation
    return os.getenv(name, "")  # No defaults, no error handling
```

### Phase 3: Common Code Testing Requirements

**Utility Testing Standards:**

- **Comprehensive edge case testing**: Test all possible input combinations
- **Error condition testing**: Verify proper error handling for invalid inputs
- **Type safety testing**: Test with various input types to verify type hints
- **Integration testing**: Test utilities in context of actual usage
- **Performance testing**: Ensure utilities don't create performance bottlenecks

**Shared Code Quality:**

- All utility functions must have corresponding unit tests
- Test coverage must be >90% for common utilities
- Include property-based testing with hypothesis for complex utilities
- Mock external dependencies in utility tests
- Test thread safety for utilities used in concurrent contexts

### Phase 4: Performance and Reusability

**Utility Performance:**

- **Caching for expensive operations**: Cache results of expensive utility calculations
- **Async where appropriate**: Use async for I/O utilities, sync for CPU-bound utilities
- **Memory efficiency**: Avoid creating unnecessary object copies in utilities
- **Algorithm efficiency**: Use appropriate data structures and algorithms

**Reusability Patterns:**

- **Generic implementations**: Write utilities that work for multiple use cases
- **Parameterizable behavior**: Allow customization through parameters, not hardcoded behavior
- **Composable utilities**: Design utilities that can be easily combined
- **Backwards compatibility**: Maintain API stability for widely-used utilities

### Phase 5: Common Code Maintainability

**Documentation and Examples:**

- **Complete documentation**: All public utilities must have comprehensive docstrings
- **Usage examples**: Include realistic examples showing typical usage patterns
- **Performance characteristics**: Document time/space complexity for non-trivial utilities
- **Thread safety**: Document whether utilities are thread-safe
- **Version compatibility**: Document any version-specific behaviors

**Code Organization:**

- **Logical grouping**: Group related utilities in appropriately named modules
- **Consistent interfaces**: Similar utilities should have consistent parameter patterns
- **Clear abstractions**: Separate interface definitions from implementations
- **Dependency management**: Minimize dependencies in common utilities

---

## Common Code Anti-Patterns

**Always Reject:**

- **Scattered constants**: Magic numbers or strings not centralized in constants.py
- **Generic exceptions**: Using `Exception`, `ValueError`, or `RuntimeError` instead of SDK-specific errors
- **Duplicate utilities**: Multiple functions doing essentially the same thing
- **Poor error handling**: Utilities without proper exception handling
- **Missing validation**: Utilities that don't validate their inputs
- **Undocumented utilities**: Shared code without proper documentation

**Constants Management Anti-Patterns:**

```python
# ❌ REJECT: Scattered constants across files
# In multiple different files:
LOCK_TTL = 300  # locks.py
DEFAULT_TIMEOUT = 300  # client.py
MAX_RETRIES = 3  # activities.py
"distributed_lock_config"  # Hardcoded string in 5 different places

# ✅ REQUIRE: Centralized constants
# In constants.py only:
DEFAULT_LOCK_TTL_SECONDS = 300
DEFAULT_DATABASE_TIMEOUT_SECONDS = 300
DEFAULT_MAX_RETRY_ATTEMPTS = 3
DISTRIBUTED_LOCK_CONFIG_KEY = "distributed_lock_config"

# Other files import from constants:
from application_sdk.constants import DISTRIBUTED_LOCK_CONFIG_KEY, DEFAULT_LOCK_TTL_SECONDS
```

**Error Handling Anti-Patterns:**

```python
# ❌ REJECT: Generic error handling
def bad_utility_function(value: str) -> dict:
    if not value:
        raise ValueError("Invalid value")  # Generic error

    try:
        result = process_value(value)
        return result
    except Exception as e:
        raise Exception(f"Processing failed: {e}")  # Generic error

# ✅ REQUIRE: SDK-specific error handling
from application_sdk.common.error_codes import ValidationError, ProcessingError

def good_utility_function(value: str) -> dict:
    """Utility function with proper error handling."""

    if not value or not value.strip():
        raise ValidationError(f"Value cannot be empty or whitespace: '{value}'")

    try:
        result = process_value(value)
        if not result:
            raise ProcessingError(f"Processing returned empty result for value: '{value}'")
        return result

    except ProcessingError:
        raise  # Re-raise SDK errors
    except Exception as e:
        raise ProcessingError(f"Unexpected error processing '{value}': {e}")
```

**Code Duplication Anti-Patterns:**

```python
# ❌ REJECT: Repeated logic in multiple files
# Found in client.py:
def setup_database_connection(host, port, user, password):
    connection_string = f"postgresql://{user}:{password}@{host}:{port}"
    return create_connection(connection_string)

# Found in activities.py:
def create_db_connection(host, port, user, password):
    conn_str = f"postgresql://{user}:{password}@{host}:{port}"
    return establish_connection(conn_str)

# ✅ REQUIRE: Extracted shared utility
# In common/utils.py:
def build_database_connection_string(
    host: str,
    port: int,
    username: str,
    password: str,
    database: Optional[str] = None,
    ssl_mode: str = "require"
) -> str:
    """
    Build a standardized database connection string.

    Used consistently across all database clients and activities.
    """
    base_url = f"postgresql://{username}:{password}@{host}:{port}"
    if database:
        base_url += f"/{database}"

    params = []
    if ssl_mode:
        params.append(f"sslmode={ssl_mode}")

    if params:
        base_url += "?" + "&".join(params)

    return base_url

# Other modules import and use the shared utility:
from application_sdk.common.utils import build_database_connection_string
```

## Educational Context for Common Code Reviews

When reviewing common code, emphasize:

1. **Consistency Impact**: "Centralized constants and utilities ensure consistency across the entire SDK. Scattered constants lead to inconsistencies and make global changes nearly impossible."

2. **Maintainability Impact**: "Well-designed utilities reduce code duplication and make the codebase easier to maintain. Changes to common functionality only need to be made in one place."

3. **Error Handling Impact**: "SDK-specific exceptions provide clearer error messages and enable better error handling throughout the application. Generic exceptions hide the root cause and make debugging difficult."

4. **Reusability Impact**: "Properly designed common utilities can be reused across multiple contexts, reducing development time and ensuring consistent behavior."

5. **Performance Impact**: "Shared utilities are called frequently throughout the application. Performance issues in common code have amplified impact across the entire system."

6. **Testing Impact**: "Common utilities require especially thorough testing because they're used in many contexts. Bugs in utilities affect multiple parts of the system simultaneously."
