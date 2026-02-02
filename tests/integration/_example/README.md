# Example Integration Test

This is a complete, working example of integration tests using the Apps-SDK
integration testing framework. Copy this directory as a starting point for
your connector's integration tests.

## Quick Start

### 1. Set Environment Variables

```bash
# Server configuration
export APP_SERVER_URL=http://localhost:8000

# Database credentials (customize for your connector)
export EXAMPLE_DB_HOST=localhost
export EXAMPLE_DB_PORT=5432
export EXAMPLE_DB_USER=test_user
export EXAMPLE_DB_PASSWORD=test_password
export EXAMPLE_DB_NAME=test_db
```

### 2. Start Your Application Server

Make sure your connector application is running:

```bash
# Example: Start your connector
python examples/application_sql.py
```

### 3. Run Tests

```bash
# Run all example tests
pytest tests/integration/_example/ -v

# Run with logging
pytest tests/integration/_example/ -v --log-cli-level=INFO

# Run specific scenario type
pytest tests/integration/_example/ -v -k "auth"

# Run single scenario
pytest tests/integration/_example/ -v -k "auth_valid_credentials"
```

## Files in This Directory

```
_example/
├── README.md              # This file
├── __init__.py            # Package marker
├── conftest.py            # Example-specific fixtures
├── scenarios.py           # Scenario definitions
└── test_integration.py    # Test class
```

### scenarios.py

Defines all test scenarios using the declarative format:

```python
from application_sdk.test_utils.integration import Scenario, lazy, equals

scenarios = [
    Scenario(
        name="auth_valid_credentials",
        api="auth",
        args=lazy(lambda: {"credentials": load_credentials()}),
        assert_that={"success": equals(True)}
    ),
    # ... more scenarios
]
```

### test_integration.py

The test class that runs scenarios:

```python
from application_sdk.test_utils.integration import BaseIntegrationTest
from .scenarios import scenarios

class ExampleIntegrationTest(BaseIntegrationTest):
    scenarios = scenarios
    server_host = "http://localhost:8000"
```

### conftest.py

Pytest fixtures specific to this example:

- `example_credentials` - Load credentials from env
- `skip_if_no_server` - Skip if server unavailable
- `skip_if_no_database` - Skip if database unavailable

## Customizing for Your Connector

### Step 1: Copy the Directory

```bash
cp -r tests/integration/_example tests/integration/my_connector
```

### Step 2: Update Credential Loading

Edit `scenarios.py` to load your connector's credentials:

```python
def load_credentials():
    return {
        # Your credential fields
        "account": os.getenv("SNOWFLAKE_ACCOUNT"),
        "username": os.getenv("SNOWFLAKE_USER"),
        "password": os.getenv("SNOWFLAKE_PASSWORD"),
        "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE"),
    }
```

### Step 3: Define Your Scenarios

Add scenarios for your connector's specific behavior:

```python
scenarios = [
    # Auth with OAuth
    Scenario(
        name="auth_oauth",
        api="auth",
        args=lazy(lambda: {
            "credentials": {
                "client_id": os.getenv("CLIENT_ID"),
                "client_secret": os.getenv("CLIENT_SECRET"),
            }
        }),
        assert_that={"success": equals(True)}
    ),
    
    # Preflight with specific config
    Scenario(
        name="preflight_with_warehouse",
        api="preflight",
        args=lazy(lambda: {
            "credentials": load_credentials(),
            "metadata": {
                "warehouse": "COMPUTE_WH",
                "databases": ["TEST_DB"],
            }
        }),
        assert_that={"success": equals(True)}
    ),
]
```

### Step 4: Update Test Class

Edit `test_integration.py`:

```python
class MyConnectorIntegrationTest(BaseIntegrationTest):
    scenarios = scenarios
    server_host = os.getenv("APP_SERVER_URL", "http://localhost:8000")
    workflow_endpoint = "/extract"  # If different from /start
    
    @classmethod
    def setup_test_environment(cls):
        # Create test data
        pass
    
    @classmethod
    def cleanup_test_environment(cls):
        # Clean up test data
        pass
```

### Step 5: Update Fixtures

Edit `conftest.py` with your connector-specific fixtures.

## Scenario Categories

### Authentication Scenarios

Test different authentication methods and edge cases:

- Valid credentials
- Invalid credentials
- Missing fields
- Expired tokens (if applicable)
- Different auth methods (OAuth, API key, etc.)

### Preflight Scenarios

Test configuration validation:

- Valid configuration
- Invalid credentials
- Missing permissions
- Non-existent resources
- Edge case configurations

### Workflow Scenarios

Test workflow execution:

- Successful extraction
- Partial failures
- Invalid configurations
- Timeout handling

## Assertion Examples

```python
from application_sdk.test_utils.integration import (
    equals, exists, one_of, contains, is_not_empty,
    greater_than, matches, all_of, any_of
)

assert_that = {
    # Basic equality
    "success": equals(True),
    
    # Check existence
    "data.workflow_id": exists(),
    
    # Check in list
    "data.status": one_of(["RUNNING", "COMPLETED"]),
    
    # String contains
    "message": contains("successful"),
    
    # Numeric comparison
    "data.count": greater_than(0),
    
    # Regex match
    "data.id": matches(r"^[a-f0-9-]+$"),
    
    # Combined assertions
    "data.name": all_of(exists(), is_not_empty()),
}
```

## Troubleshooting

### "Server not available"

Ensure your application server is running:

```bash
curl http://localhost:8000/server/health
```

### "Credentials not loading"

Check environment variables are set:

```bash
env | grep EXAMPLE_
```

### "Assertion failed"

Run with debug logging:

```bash
pytest tests/integration/_example/ -v --log-cli-level=DEBUG
```

### "Timeout"

Increase the timeout in test class:

```python
class MyTest(BaseIntegrationTest):
    timeout = 60  # Increase from default 30
```

## Best Practices

1. **Use lazy() for credentials** - Prevents failures at import time
2. **Test negative cases** - Invalid inputs, missing permissions
3. **Clean up test data** - Use setup/cleanup hooks
4. **Use descriptive names** - `auth_expired_token` not `test_3`
5. **Document scenarios** - Use the description field
6. **Skip unavailable tests** - Use skip/skip_reason fields
