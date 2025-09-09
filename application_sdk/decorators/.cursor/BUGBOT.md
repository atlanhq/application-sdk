# Decorator Code Review Guidelines - Centralized Function Decorators

## Context-Specific Patterns

This directory contains all decorator implementations for the Application SDK. Decorators must be centralized here to avoid scattered functionality and ensure consistent patterns.

### Phase 1: Critical Decorator Safety Issues

**Decorator Centralization:**

- **ALL decorators must be in this directory**: No decorators should exist in other modules (lock/, observability/, etc.)
- **Consolidate scattered decorators**: If decorators are found elsewhere, they must be moved here
- **Single responsibility per file**: Each decorator type should have its own file (locks.py, observability_decorator.py)
- **Proper imports**: Other modules should import decorators from here, not define their own

**Type Safety and Function Signatures:**

- All decorators must preserve function signatures and type hints
- Use `functools.wraps` to maintain function metadata
- Generic decorators must use proper type annotations
- Return types must match the original function's return type

```python
# ✅ DO: Proper decorator type safety
from typing import Callable, Any, TypeVar, ParamSpec
from functools import wraps

P = ParamSpec('P')
T = TypeVar('T')

def my_decorator(func: Callable[P, T]) -> Callable[P, T]:
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        # Decorator logic
        return func(*args, **kwargs)
    return wrapper

# ❌ NEVER: Poor type annotations
def bad_decorator(func):  # No type hints
    def wrapper(*args, **kwargs):  # No type preservation
        return func(*args, **kwargs)
    return wrapper  # Missing @wraps
```

### Phase 2: Decorator Architecture Patterns

**Proper Decorator Structure:**

- **Parameterized decorators**: Support both `@decorator` and `@decorator(param=value)` usage patterns
- **Error handling**: Decorators must not swallow exceptions unless explicitly designed to do so
- **Resource cleanup**: Decorators that acquire resources must ensure cleanup in finally blocks
- **Context preservation**: Maintain original function context and metadata

**Configuration Management:**

- **Centralized constants**: All decorator configuration should use constants from this directory
- **Shared configuration**: Related decorators should share configuration patterns
- **Environment awareness**: Decorators should work in both development and production environments

```python
# ✅ DO: Proper decorator configuration
from application_sdk.constants import DEFAULT_LOCK_TTL, DEFAULT_MAX_LOCKS

# Shared configuration for lock decorators
LOCK_CONFIG_KEY = "distributed_lock_config"  # Centralized key

def distributed_lock(
    lock_name: Optional[str] = None,
    max_locks: int = DEFAULT_MAX_LOCKS,
    ttl_seconds: int = DEFAULT_LOCK_TTL
):
    """Distributed lock decorator with proper defaults and configuration."""

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            # Use centralized configuration
            actual_lock_name = lock_name or f"{func.__module__}.{func.__name__}"

            # Store config in activity context using shared key
            activity_info = activity.info()
            activity_info.memo[LOCK_CONFIG_KEY] = {
                "lock_name": actual_lock_name,
                "max_locks": max_locks,
                "ttl_seconds": ttl_seconds
            }

            return await func(*args, **kwargs)
        return wrapper
    return decorator

# ❌ REJECT: Scattered constants and configuration
def bad_lock_decorator(max_locks=10):  # Hardcoded default
    LOCK_KEY = "my_lock_key"  # Should be centralized
    # Configuration scattered across files
```

### Phase 3: Decorator Testing Requirements

**Comprehensive Decorator Testing:**

- **Function preservation**: Test that decorators preserve original function behavior
- **Type safety**: Verify type hints are maintained after decoration
- **Error propagation**: Ensure exceptions are properly handled and propagated
- **Resource cleanup**: Test cleanup behavior in both success and failure cases
- **Configuration validation**: Test all configuration parameters and edge cases

```python
# ✅ DO: Comprehensive decorator testing
@pytest.mark.asyncio
class TestDistributedLockDecorator:
    """Test suite for distributed lock decorator."""

    async def test_function_signature_preservation(self):
        """Test that decorator preserves function signature and types."""

        @distributed_lock("test_lock")
        async def test_function(param1: str, param2: int = 10) -> dict:
            """Test function docstring."""
            return {"param1": param1, "param2": param2}

        # Verify signature preservation
        assert test_function.__name__ == "test_function"
        assert test_function.__doc__ == "Test function docstring."

        # Verify function still works
        result = await test_function("test", 20)
        assert result == {"param1": "test", "param2": 20}

    async def test_error_propagation(self):
        """Test that decorator properly propagates exceptions."""

        @distributed_lock("error_lock")
        async def failing_function():
            raise ValueError("Test error")

        # Verify exception is propagated, not swallowed
        with pytest.raises(ValueError, match="Test error"):
            await failing_function()

    async def test_resource_cleanup_on_failure(self, mock_lock_manager):
        """Test that resources are cleaned up even when function fails."""

        @distributed_lock("cleanup_test")
        async def failing_function():
            raise RuntimeError("Simulated failure")

        mock_lock_manager.acquire_lock.return_value.__aenter__ = AsyncMock()
        mock_lock_manager.acquire_lock.return_value.__aexit__ = AsyncMock()

        with pytest.raises(RuntimeError):
            await failing_function()

        # Verify cleanup was called
        mock_lock_manager.acquire_lock.return_value.__aexit__.assert_called_once()
```

### Phase 4: Performance and Integration

**Decorator Performance:**

- **Minimal overhead**: Decorators should add minimal performance overhead
- **Async compatibility**: All decorators must work correctly with async functions
- **Context manager efficiency**: Use efficient context managers for resource management
- **Caching**: Cache expensive decorator setup operations where appropriate

**Integration Patterns:**

- **Temporal integration**: Decorators must work correctly with Temporal activities and workflows
- **Observability integration**: Integrate with logging, metrics, and tracing systems
- **Error handling integration**: Work correctly with the SDK's error handling patterns

### Phase 5: Decorator Maintainability

**Code Organization:**

- **One decorator type per file**: Keep related decorators together (all lock decorators in locks.py)
- **Clear naming**: Decorator files should clearly indicate their purpose
- **Consistent patterns**: All decorators should follow the same structural patterns
- **Documentation**: Each decorator must have comprehensive docstrings with usage examples

**Backwards Compatibility:**

- **API stability**: Decorator APIs should be stable across versions
- **Graceful deprecation**: Deprecated decorators should include migration guidance
- **Version compatibility**: Support existing usage patterns when adding new features

---

## Decorator-Specific Anti-Patterns

**Always Reject:**

- **Scattered decorators**: Decorators defined outside this directory
- **Missing type safety**: Decorators without proper type annotations
- **Resource leaks**: Decorators that don't clean up resources properly
- **Exception swallowing**: Decorators that hide exceptions unintentionally
- **Poor configuration**: Hardcoded values that should be configurable
- **No function preservation**: Decorators that don't preserve original function metadata

**Centralization Anti-Patterns:**

```python
# ❌ REJECT: Decorators in wrong locations
# Found in application_sdk/lock/__init__.py
def needs_lock(max_locks=10):
    """Should be in decorators/locks.py instead"""

# Found in application_sdk/observability/some_module.py
def trace_activity(func):
    """Should be in decorators/observability_decorator.py"""

# ✅ REQUIRE: Centralized decorators
# In application_sdk/decorators/locks.py
def needs_lock(max_locks: int = DEFAULT_MAX_LOCKS):
    """Properly located distributed lock decorator"""

# In application_sdk/decorators/observability_decorator.py
def observability(logger=None, metrics=None, traces=None):
    """Properly located observability decorator"""
```

**Type Safety Anti-Patterns:**

```python
# ❌ REJECT: Poor type safety
def bad_decorator(func):  # No type annotations
    def wrapper(*args, **kwargs):  # No parameter specifications
        return func(*args, **kwargs)
    return wrapper  # Missing @wraps, no return type

# ✅ REQUIRE: Proper type safety
from typing import Callable, TypeVar, ParamSpec
from functools import wraps

P = ParamSpec('P')
T = TypeVar('T')

def good_decorator(func: Callable[P, T]) -> Callable[P, T]:
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return func(*args, **kwargs)
    return wrapper
```

**Configuration Anti-Patterns:**

```python
# ❌ REJECT: Scattered configuration
# Different files using different keys for same concept
LOCK_KEY_1 = "lock_config"  # In locks.py
LOCK_KEY_2 = "distributed_lock"  # In interceptors.py
DEFAULT_TTL = 300  # Hardcoded in decorator

# ✅ REQUIRE: Centralized configuration
# In application_sdk/constants.py
DISTRIBUTED_LOCK_CONFIG_KEY = "distributed_lock_config"
DEFAULT_LOCK_TTL = 300
DEFAULT_MAX_LOCKS = 10

# In decorators using shared constants
from application_sdk.constants import DISTRIBUTED_LOCK_CONFIG_KEY, DEFAULT_LOCK_TTL
```

## Educational Context for Decorator Reviews

When reviewing decorator code, emphasize:

1. **Centralization Impact**: "Scattered decorators create maintenance nightmares. When the same decorator logic appears in multiple places, bugs get fixed in some places but not others. Centralization ensures consistency and reduces maintenance burden."

2. **Type Safety Impact**: "Decorators that don't preserve type information break IDE support, static analysis, and developer productivity. Proper type annotations are essential for maintaining code quality in large codebases."

3. **Resource Management Impact**: "Decorators often manage resources (locks, connections, contexts). Poor resource management in decorators can cause system-wide issues because they're used across many functions."

4. **Function Preservation Impact**: "Decorators that don't preserve original function metadata break debugging, introspection, and documentation tools. Using @functools.wraps is not optional."

5. **Testing Impact**: "Decorators are cross-cutting concerns that affect many functions. Bugs in decorators have amplified impact, making thorough testing especially critical."

6. **Performance Impact**: "Decorators add overhead to every function call they wrap. Inefficient decorators can degrade system performance across the entire application."
