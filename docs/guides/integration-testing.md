# Integration Testing Guide

This guide explains how to write integration tests for your connector using the Apps-SDK integration testing framework.

## Overview

The integration testing framework provides a **declarative, data-driven** approach to testing. Instead of writing procedural test code, you define **scenarios** that specify:

- What API to test
- What inputs to provide
- What outputs to expect

The framework handles the rest: calling APIs, validating assertions, and reporting results.

## Why Use This Framework?

### For External Developers

- **Easy to Use**: Define scenarios as data, not code
- **Minimal Python Knowledge**: Just fill in the template
- **Comprehensive Coverage**: Test auth, preflight, and workflow APIs
- **Consistent Quality**: Same test structure across all connectors

### For TDD (Test-Driven Development)

- **Scenarios = Specification**: Write what should happen before implementing
- **Fast Feedback**: Run tests frequently during development
- **Regression Prevention**: Ensure changes don't break existing functionality

## Quick Start

### Step 1: Copy the Example

```bash
cp -r tests/integration/_example tests/integration/my_connector
```

### Step 2: Define Your Scenarios

Edit `scenarios.py`:

```python
from application_sdk.test_utils.integration import (
    Scenario, lazy, equals, exists
)

def load_credentials():
    return {
        "host": os.getenv("MY_DB_HOST"),
        "username": os.getenv("MY_DB_USER"),
        "password": os.getenv("MY_DB_PASSWORD"),
    }

scenarios = [
    Scenario(
        name="auth_valid",
        api="auth",
        args=lazy(lambda: {"credentials": load_credentials()}),
        assert_that={"success": equals(True)}
    ),
]
```

### Step 3: Create Test Class

Edit `test_integration.py`:

```python
from application_sdk.test_utils.integration import BaseIntegrationTest
from .scenarios import scenarios

class MyConnectorTest(BaseIntegrationTest):
    scenarios = scenarios
    server_host = "http://localhost:8000"
```

### Step 4: Run Tests

```bash
export MY_DB_HOST=localhost
export MY_DB_USER=test
export MY_DB_PASSWORD=secret
export APP_SERVER_URL=http://localhost:8000

pytest tests/integration/my_connector/ -v
```

## Core Concepts

### Scenarios

A **Scenario** defines a single test case:

```python
Scenario(
    name="auth_valid_credentials",      # Unique identifier
    api="auth",                          # API to test
    args={"credentials": {...}},         # Input arguments
    assert_that={"success": equals(True)} # Expected outcomes
)
```

### Supported APIs

| API | Endpoint | Purpose |
|-----|----------|---------|
| `auth` | `/workflows/v1/auth` | Test authentication |
| `preflight` | `/workflows/v1/check` | Validate configuration |
| `workflow` | `/workflows/v1/{endpoint}` | Start workflow |

### Lazy Evaluation

Use `lazy()` to defer computation until test execution:

```python
# BAD: Loads at import time (fails if env vars missing)
args={"credentials": load_credentials()}

# GOOD: Loads when test runs
args=lazy(lambda: {"credentials": load_credentials()})
```

Benefits:
- Tests can be defined in one environment, run in another
- Credentials loaded only when needed
- Values cached after first evaluation

### Assertion DSL

The assertion DSL provides **higher-order functions** that return predicates:

```python
from application_sdk.test_utils.integration import (
    equals, exists, one_of, contains, greater_than
)

assert_that = {
    "success": equals(True),
    "data.workflow_id": exists(),
    "data.status": one_of(["RUNNING", "COMPLETED"]),
    "message": contains("successful"),
    "data.count": greater_than(0),
}
```

## Assertion Reference

### Basic Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `equals(value)` | Exact equality | `equals(True)` |
| `not_equals(value)` | Not equal | `not_equals(None)` |
| `exists()` | Not None | `exists()` |
| `is_none()` | Is None | `is_none()` |
| `is_true()` | Truthy value | `is_true()` |
| `is_false()` | Falsy value | `is_false()` |

### Collection Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `one_of(list)` | Value in list | `one_of(["a", "b"])` |
| `not_one_of(list)` | Value not in list | `not_one_of(["error"])` |
| `contains(item)` | Contains item | `contains("success")` |
| `not_contains(item)` | Doesn't contain | `not_contains("error")` |
| `has_length(n)` | Length equals n | `has_length(3)` |
| `is_empty()` | Empty collection | `is_empty()` |
| `is_not_empty()` | Non-empty | `is_not_empty()` |

### Numeric Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `greater_than(n)` | Greater than | `greater_than(0)` |
| `greater_than_or_equal(n)` | >= | `greater_than_or_equal(1)` |
| `less_than(n)` | Less than | `less_than(100)` |
| `less_than_or_equal(n)` | <= | `less_than_or_equal(10)` |
| `between(min, max)` | In range | `between(1, 10)` |

### String Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `matches(pattern)` | Regex match | `matches(r"^[a-z]+$")` |
| `starts_with(prefix)` | Starts with | `starts_with("http")` |
| `ends_with(suffix)` | Ends with | `ends_with(".json")` |

### Type Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `is_type(type)` | Instance check | `is_type(str)` |
| `is_dict()` | Is dictionary | `is_dict()` |
| `is_list()` | Is list | `is_list()` |
| `is_string()` | Is string | `is_string()` |

### Combinators

Combine multiple assertions:

```python
from application_sdk.test_utils.integration import all_of, any_of, none_of

# All must pass
"data.name": all_of(exists(), is_string(), is_not_empty())

# At least one must pass  
"data.role": any_of(equals("admin"), equals("superuser"))

# None should pass
"message": none_of(contains("error"), contains("fail"))
```

### Custom Assertions

Create your own:

```python
from application_sdk.test_utils.integration import custom

# Using custom()
"data.count": custom(lambda x: x % 2 == 0, "is_even")

# Or directly as a lambda
"data.value": lambda x: x > 0 and x < 100
```

## Writing Effective Scenarios

### Auth Scenarios

Test different authentication methods and edge cases:

```python
auth_scenarios = [
    # Valid credentials
    Scenario(
        name="auth_valid",
        api="auth",
        args=lazy(lambda: {"credentials": load_credentials()}),
        assert_that={"success": equals(True)}
    ),
    
    # Invalid password
    Scenario(
        name="auth_invalid_password",
        api="auth",
        args=lazy(lambda: {
            "credentials": {**load_credentials(), "password": "wrong"}
        }),
        assert_that={"success": equals(False)}
    ),
    
    # Empty credentials
    Scenario(
        name="auth_empty",
        api="auth",
        args={"credentials": {}},
        assert_that={"success": equals(False)}
    ),
]
```

### Preflight Scenarios

Test configuration validation:

```python
preflight_scenarios = [
    # Valid configuration
    Scenario(
        name="preflight_valid",
        api="preflight",
        args=lazy(lambda: {
            "credentials": load_credentials(),
            "metadata": {"databases": ["TEST_DB"]}
        }),
        assert_that={"success": equals(True)}
    ),
    
    # Non-existent database
    Scenario(
        name="preflight_bad_database",
        api="preflight",
        args=lazy(lambda: {
            "credentials": load_credentials(),
            "metadata": {"databases": ["NONEXISTENT"]}
        }),
        assert_that={"success": equals(False)}
    ),
]
```

### Workflow Scenarios

Test workflow execution:

```python
workflow_scenarios = [
    # Successful workflow
    Scenario(
        name="workflow_success",
        api="workflow",
        args=lazy(lambda: {
            "credentials": load_credentials(),
            "metadata": {"databases": ["TEST_DB"]},
            "connection": {"name": "test_conn"}
        }),
        assert_that={
            "success": equals(True),
            "data.workflow_id": exists(),
            "data.run_id": exists(),
        }
    ),
]
```

## Test Class Configuration

### Basic Configuration

```python
class MyConnectorTest(BaseIntegrationTest):
    scenarios = scenarios
    server_host = "http://localhost:8000"
    server_version = "v1"
    workflow_endpoint = "/start"
    timeout = 30
```

### Dynamic Workflow Endpoint

If your workflow endpoint is different from `/start`:

```python
class MyConnectorTest(BaseIntegrationTest):
    scenarios = scenarios
    workflow_endpoint = "/extract"  # Custom endpoint
```

Or per-scenario:

```python
Scenario(
    name="workflow_custom_endpoint",
    api="workflow",
    endpoint="/custom/start",  # Override for this scenario
    args={...},
    assert_that={...}
)
```

### Setup and Teardown Hooks

```python
class MyConnectorTest(BaseIntegrationTest):
    scenarios = scenarios
    
    @classmethod
    def setup_test_environment(cls):
        """Called before any tests run."""
        # Create test database, schema, etc.
        cls.db = create_database_connection()
        cls.db.execute("CREATE SCHEMA test_schema")
    
    @classmethod
    def cleanup_test_environment(cls):
        """Called after all tests complete."""
        # Drop test database, clean up
        cls.db.execute("DROP SCHEMA test_schema CASCADE")
        cls.db.close()
    
    def before_scenario(self, scenario):
        """Called before each scenario."""
        print(f"Running: {scenario.name}")
    
    def after_scenario(self, scenario, result):
        """Called after each scenario."""
        status = "PASSED" if result.success else "FAILED"
        print(f"{scenario.name}: {status}")
```

## Running Tests

### Basic Execution

```bash
# All integration tests
pytest tests/integration/ -v

# Specific connector
pytest tests/integration/my_connector/ -v

# Single scenario
pytest tests/integration/my_connector/ -v -k "auth_valid"
```

### With Logging

```bash
# INFO level
pytest tests/integration/ -v --log-cli-level=INFO

# DEBUG level (shows API responses)
pytest tests/integration/ -v --log-cli-level=DEBUG
```

### Skip Slow Tests

Mark scenarios to skip:

```python
Scenario(
    name="workflow_large_extraction",
    api="workflow",
    args={...},
    assert_that={...},
    skip=True,
    skip_reason="Takes too long for CI"
)
```

## Best Practices

### 1. Use Lazy Evaluation for Credentials

```python
# Always use lazy() for credentials
args=lazy(lambda: {"credentials": load_credentials()})
```

### 2. Test Negative Cases

Don't just test the happy path:

```python
scenarios = [
    # Happy path
    Scenario(name="auth_valid", ...),
    
    # Negative cases
    Scenario(name="auth_invalid_password", ...),
    Scenario(name="auth_empty_credentials", ...),
    Scenario(name="auth_missing_username", ...),
]
```

### 3. Use Descriptive Names

```python
# Good names
"auth_invalid_password"
"preflight_missing_permissions"
"workflow_large_dataset"

# Bad names
"test_1"
"scenario_a"
```

### 4. Document Complex Scenarios

```python
Scenario(
    name="preflight_partial_permissions",
    description="Test when user has read but not write permissions",
    api="preflight",
    args={...},
    assert_that={...}
)
```

### 5. Clean Up Test Data

Use hooks to manage test data:

```python
@classmethod
def setup_test_environment(cls):
    cls.test_data = create_test_data()

@classmethod  
def cleanup_test_environment(cls):
    delete_test_data(cls.test_data)
```

## Troubleshooting

### "Server not available"

Check server is running:
```bash
curl http://localhost:8000/server/health
```

### "Credentials not loading"

Verify environment variables:
```bash
env | grep MY_DB_
```

### "Assertion failed"

Run with debug logging:
```bash
pytest -v --log-cli-level=DEBUG
```

### "Timeout"

Increase timeout:
```python
class MyTest(BaseIntegrationTest):
    timeout = 60  # Increase from default 30
```

## Example Directory Structure

```
tests/integration/
├── __init__.py
├── conftest.py              # Shared fixtures
├── README.md
├── _example/                # Reference example
│   ├── __init__.py
│   ├── conftest.py
│   ├── scenarios.py
│   ├── test_integration.py
│   └── README.md
└── my_connector/            # Your connector tests
    ├── __init__.py
    ├── conftest.py
    ├── scenarios.py
    └── test_integration.py
```

## Summary

1. **Copy the example**: Start from `tests/integration/_example/`
2. **Define scenarios**: Edit `scenarios.py` with your test cases
3. **Create test class**: Inherit from `BaseIntegrationTest`
4. **Set environment variables**: Configure credentials
5. **Run tests**: `pytest tests/integration/my_connector/ -v`

The framework handles the complexity of API calls, response validation, and reporting. You focus on defining what to test and what to expect.
