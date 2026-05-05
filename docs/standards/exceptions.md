# Exception Handling Standards

## Core Principles

**Default Behavior: Re-raise Exceptions** - Exceptions should be re-raised by default unless explicitly handled for a specific reason. Silent exception swallowing is not allowed.

## Critical Rules

### **Exception Propagation**
- **Rule**: Always re-raise exceptions after logging unless in non-critical operations
- **Anti-pattern**: `except Exception as e: logger.error(f"Error: {e}")` — swallows exception, f-string style, missing `exc_info=True`
- **Correct pattern**: `except Exception as e: logger.error("Error: %s", e, exc_info=True); raise`

### **Specific Exception Types**
- **Use specific exception types** instead of generic `Exception`
- **Examples**: `ValueError`, `ConnectionError`, `TimeoutError`, `FileNotFoundError`
- **Create custom exceptions** in `application_sdk/errors.py` (top-level structured error codes, format `AAF-{COMP}-{ID:03d}`) for domain-specific errors (the legacy `application_sdk/common/error_codes.py` is retained for backward compatibility only)
- **Anti-pattern**: `except Exception:` - Too broad, masks real issues

## Exception Handling Patterns

### **DO: Proper exception handling with re-raising**
```python
try:
    result = some_operation()
    return result
except ValueError as e:
    logger.error("Invalid input: %s", e, exc_info=True)
    raise
except ConnectionError as e:
    logger.error("Connection failed: %s", e, exc_info=True)
    raise
except Exception as e:
    logger.error("Unexpected error: %s", e, exc_info=True)
    raise
```

### **DON'T: Swallowing exceptions**
```python
try:
    result = some_operation()
    return result
except Exception as e:
    logger.error(f"Error: {e}")
    # Missing raise - this swallows the exception!
```

## Context-Specific Guidelines

### **1. Critical Operations (Database, Network, File I/O)**
- **Always re-raise** exceptions after logging
- **Include context** in error messages
- **Use specific exception types**

### **2. Non-Critical Operations (Logging, Metrics, Observability)**
- **May swallow exceptions** to prevent cascading failures
- **Log the failure** for debugging
- **Continue operation** if possible

### **3. Resource Cleanup**
- **Use try/finally** for guaranteed cleanup
- **Log cleanup failures** but don't re-raise (to avoid masking original error)

## Common Anti-patterns to Reject

### **Silent Exception Swallowing**
```python
try:
    operation()
except Exception:
    pass  # REJECT: Silent failure
```

### **Generic Exception Catching Without Re-raising**
```python
try:
    operation()
except Exception as e:
    logger.error(f"Error: {e}")  # REJECT: swallows exception, f-string style, missing exc_info=True
```

### **Overly Broad Exception Handling**
```python
try:
    operation()
except Exception as e:  # REJECT: too broad — use specific types when possible
    logger.error(f"Error: {e}")  # REJECT: f-string style, missing exc_info=True
    raise
```

### **Exception Handling Without Context**
```python
try:
    operation()
except Exception as e:
    logger.error(f"Error: {e}")  # REJECT: f-string style, missing exc_info=True, no operation context
    raise
```

## Best Practices

### **DO: Proper exception handling with context**
```python
try:
    result = database_connection.execute_query(query)
    return result
except ConnectionError as e:
    logger.error("Database connection failed for query %s: %s", query[:50], e, exc_info=True)
    raise
except ValueError as e:
    logger.error("Invalid query parameters: %s", e, exc_info=True)
    raise
except Exception as e:
    logger.error("Unexpected error during database operation: %s", e, exc_info=True)
    raise
```

### **DO: Resource cleanup with exception handling**
```python
file_handle = None
try:
    file_handle = open(filename, 'r')
    return file_handle.read()
except FileNotFoundError as e:
    logger.error("File not found: %s", filename, exc_info=True)
    raise
finally:
    if file_handle:
        try:
            file_handle.close()
        except Exception as e:
            logger.warning("Failed to close file %s: %s", filename, e, exc_info=True)
            # Don't re-raise cleanup errors
```

### **DO: Non-critical operation exception handling**
```python
try:
    metrics.record_metric("operation_success", 1)
except Exception as e:
    logger.warning("Failed to record metric: %s", e, exc_info=True)
    # Don't re-raise for non-critical operations
```

## Valid Exception Swallowing in This Codebase

The following patterns are acceptable and used in the codebase:

### **VALID: Signal Handlers and Cleanup Operations**
```python
# Illustrative — cleanup/flush errors that must not crash the process
except Exception as e:
    logger.warning("Error during signal handler flush: %s", e, exc_info=True)
    # Don't re-raise - cleanup failures shouldn't crash the application
```

### **VALID: Observability Operations (Metrics, Traces, Events)**
```python
# From observability_decorator.py - trace recording
try:
    traces.record_trace(...)
except Exception as trace_error:
    logger.warning("Failed to record trace: %s", trace_error, exc_info=True)
    # Don't re-raise - observability failures shouldn't break business logic

# From interceptors/events.py - event publishing
except Exception as publish_error:
    logger.warning("Failed to publish workflow end event: %s", publish_error, exc_info=True)
    # Don't re-raise - event publishing is non-critical
```

### **VALID: Cleanup in Finally Blocks**
```python
# Illustrative — cleanup inside finally so workflow end is non-fatal
finally:
    try:
        await workflow.execute_activity(cleanup, ...)
    except Exception as e:
        logger.warning("Failed to cleanup artifacts: %s", e, exc_info=True)
        # Don't re-raise - cleanup failures shouldn't fail the workflow
```

## Module-Specific Guidelines

### **Handlers (`application_sdk/handler/`)**
- **Critical**: Always re-raise exceptions after logging
- **Include operation context** in error messages
- **Use specific exception types** for different error scenarios

### **Storage (`application_sdk/storage/`) and Outputs (`application_sdk/outputs/`)**
- **Critical**: Re-raise exceptions for data reading/writing operations
- **Include file/connection context** in error messages
- **Handle specific I/O exceptions** appropriately
- **Resource cleanup**: Ensure files/connections are properly closed

### **Observability (`application_sdk/observability/`)**
- **Non-critical**: May swallow exceptions to prevent cascading failures
- **Log all failures** for debugging
- **Continue operation** when possible

### **Apps (`application_sdk/app/`)**
- **Critical**: Re-raise exceptions to trigger workflow retry logic
- **Include workflow context** in error messages
- **Handle Temporal-specific exceptions** appropriately

### **Server middleware (`application_sdk/server/`)**
- **Non-critical for middleware**: Middleware, MCP plumbing, and FastAPI utilities — swallow or log non-fatal errors; let the framework handle unhandled ones
- **Include request context** in error messages
- **Note**: HTTP request-handler error handling belongs in `application_sdk/handler/` (see "Handlers" section above)

## Review Checklist

When reviewing code, check for:

1. **Exception Propagation**: Are exceptions properly re-raised when they should be?
2. **Specific Exception Types**: Are specific exception types used instead of generic `Exception`?
3. **Error Context**: Do error messages include sufficient context for debugging?
4. **Resource Cleanup**: Are resources properly cleaned up in finally blocks?
5. **Non-Critical Operations**: Are non-critical operations (logging, metrics) handled appropriately?
6. **Custom Exceptions**: Are domain-specific exceptions defined in `application_sdk/errors.py`?
7. **Exception Documentation**: Are exceptions documented in function docstrings?

## Implementation Notes

- **Custom Exceptions**: Define new structured error codes in `application_sdk/errors.py` (format: `AAF-{COMP}-{ID:03d}`). The legacy `application_sdk/common/error_codes.py` is retained for backward compatibility only.
- **Logging**: Use `AtlanLoggerAdapter` for all logging with proper context
- **Context**: Always include relevant context in error messages (query, filename, operation, etc.)
- **Documentation**: Document all exceptions that functions can raise in docstrings
- **Temporal task boundaries**: When a task needs Temporal to classify the failure (e.g. mark non-retryable or tag with a failure type), use `ApplicationError` from `application_sdk.execution` instead of `AppError`:
  ```python
  from application_sdk.execution import ApplicationError
  raise ApplicationError("invalid schema", type="ValidationError", non_retryable=True)
  ```
  Use `AppError` for framework-level errors that Temporal does not need to classify.
