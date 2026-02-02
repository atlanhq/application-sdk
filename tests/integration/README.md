# Integration Tests

This directory contains integration tests for Apps-SDK connectors.

## Quick Start

### 1. Copy the Example

```bash
cp -r tests/integration/_example tests/integration/my_connector
```

### 2. Configure Your Scenarios

Edit `tests/integration/my_connector/scenarios.py`:

```python
from application_sdk.test_utils.integration import Scenario, lazy, equals, exists

def load_my_credentials():
    return {
        "host": os.getenv("MY_DB_HOST"),
        "username": os.getenv("MY_DB_USER"),
        "password": os.getenv("MY_DB_PASSWORD"),
    }

scenarios = [
    Scenario(
        name="auth_valid",
        api="auth",
        args=lazy(lambda: {"credentials": load_my_credentials()}),
        assert_that={"success": equals(True)}
    ),
    # Add more scenarios...
]
```

### 3. Set Environment Variables

```bash
export MY_DB_HOST=localhost
export MY_DB_USER=test
export MY_DB_PASSWORD=secret
export APP_SERVER_URL=http://localhost:8000
```

### 4. Run Tests

```bash
# Run all integration tests
pytest tests/integration/ -v

# Run specific connector tests
pytest tests/integration/my_connector/ -v

# Run with verbose output
pytest tests/integration/my_connector/ -v --log-cli-level=INFO
```

## Directory Structure

```
tests/integration/
├── __init__.py
├── conftest.py           # Shared fixtures
├── README.md             # This file
└── _example/             # Reference example (copy this)
    ├── README.md
    ├── scenarios.py      # Scenario definitions
    ├── test_integration.py
    └── conftest.py
```

## Writing Scenarios

A scenario defines:
- **name**: Unique identifier
- **api**: Which API to test ("auth", "preflight", "workflow")
- **args**: Input arguments (can be lazy-evaluated)
- **assert_that**: Expected outcomes using assertion DSL

### Example Scenarios

```python
from application_sdk.test_utils.integration import (
    Scenario, lazy, equals, exists, one_of, is_not_empty
)

scenarios = [
    # Valid authentication
    Scenario(
        name="auth_valid_credentials",
        api="auth",
        args=lazy(lambda: {"credentials": load_credentials()}),
        assert_that={
            "success": equals(True),
            "message": equals("Authentication successful"),
        }
    ),
    
    # Invalid authentication
    Scenario(
        name="auth_invalid_credentials",
        api="auth",
        args={"credentials": {"username": "wrong", "password": "wrong"}},
        assert_that={
            "success": equals(False),
        }
    ),
    
    # Preflight check
    Scenario(
        name="preflight_valid",
        api="preflight",
        args=lazy(lambda: {
            "credentials": load_credentials(),
            "metadata": {"databases": ["TEST_DB"]}
        }),
        assert_that={
            "success": equals(True),
        }
    ),
    
    # Workflow execution
    Scenario(
        name="workflow_full_extraction",
        api="workflow",
        args=lazy(lambda: {
            "credentials": load_credentials(),
            "metadata": {"databases": ["TEST_DB"]},
            "connection": {"name": "test_conn"}
        }),
        assert_that={
            "success": equals(True),
            "data.workflow_id": exists(),
        }
    ),
]
```

## Assertion DSL Reference

### Basic Assertions
- `equals(value)` - Exact equality
- `not_equals(value)` - Not equal
- `exists()` - Not None
- `is_none()` - Is None
- `is_true()` - Truthy
- `is_false()` - Falsy

### Collection Assertions
- `one_of([...])` - Value in list
- `contains(item)` - Contains item
- `has_length(n)` - Length equals n
- `is_empty()` - Empty collection
- `is_not_empty()` - Non-empty

### String Assertions
- `matches(regex)` - Regex match
- `starts_with(prefix)` - Starts with
- `ends_with(suffix)` - Ends with

### Numeric Assertions
- `greater_than(n)` - Greater than
- `less_than(n)` - Less than
- `between(min, max)` - In range

### Combinators
- `all_of(p1, p2, ...)` - All pass
- `any_of(p1, p2, ...)` - Any passes
- `none_of(p1, p2, ...)` - None pass

### Custom
- `custom(fn)` - User function

## Lazy Evaluation

Use `lazy()` to defer credential loading until test execution:

```python
# BAD: Loads immediately at import time
args={"credentials": load_credentials()}  # Fails if env vars not set

# GOOD: Loads when test runs
args=lazy(lambda: {"credentials": load_credentials()})  # Deferred
```

## Troubleshooting

### Tests not finding server
- Ensure server is running: `curl http://localhost:8000/server/health`
- Check APP_SERVER_URL environment variable

### Credentials not loading
- Verify environment variables are set
- Use `lazy()` for deferred loading

### Assertions failing
- Check response structure with `--log-cli-level=DEBUG`
- Verify paths in assert_that match response keys

## Best Practices

1. **Use lazy() for credentials** - Don't load at import time
2. **Test negative cases** - Invalid credentials, missing data
3. **Keep scenarios focused** - One thing per scenario
4. **Use descriptive names** - `auth_invalid_password` not `test_1`
5. **Clean up test data** - Use setup/teardown hooks
