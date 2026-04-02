# Write Integration Tests

You are helping a developer write integration tests for an Atlan connector using the `BaseIntegrationTest` framework from `application_sdk`.

## Your Job

Generate a complete `tests/integration/test_{connector}_integration.py` file, plus `tests/integration/__init__.py` if it doesn't exist.

## Step 1: Gather Context

Before writing any tests, read these files to understand the connector:

1. **`.env`** or **`.env.example`** — find `ATLAN_APPLICATION_NAME` and any `E2E_*` variables already defined
2. **`main.py`** — understand the app structure
3. **`pyproject.toml`** — check the project name for context
4. **Any existing `tests/integration/` files** — avoid duplicating work

From the `.env`, extract:
- `ATLAN_APPLICATION_NAME` → this is the `APP_NAME` (e.g. `postgres`, `mysql`, `snowflake`)
- Default port for this connector type
- Any connector-specific credential fields (e.g. `sslmode`, `warehouse`, `role`)

## Step 2: Understand the Framework

### File Location
```
tests/
└── integration/
    ├── __init__.py          (empty, just marks it as a package)
    └── test_{app_name}_integration.py
```

### Import Pattern
```python
from application_sdk.test_utils.integration import (
    BaseIntegrationTest,
    Scenario,
    contains,
    equals,
    exists,
    is_dict,
    is_string,
    is_true,
    matches,
    # add others as needed
)
```

### Class Structure
```python
class Test{ConnectorName}Integration(BaseIntegrationTest):
    """Integration tests for {ConnectorName} connector.

    Credentials are auto-loaded from E2E_{APP_NAME}_* env vars.
    Server URL is auto-discovered from ATLAN_APP_HTTP_HOST/PORT.
    Each scenario becomes its own pytest test.
    """

    # Fields merged with auto-discovered env creds for every scenario
    default_credentials = {
        "authType": "basic",
        "type": "all",
        # any other always-needed fields
    }

    # Used for all preflight/workflow scenarios
    default_metadata = {
        "exclude-filter": "{}",
        "include-filter": '{"^{db}$": ["^{schema}$"]}',
        "temp-table-regex": "",
        "extraction-method": "direct",
    }

    # Used for all workflow scenarios
    default_connection = {
        "connection_name": "test_connection",
        "connection_qualified_name": "default/{app_name}/test_integration",
    }

    scenarios = [...]
```

### Scenario Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Snake_case unique ID. Becomes `test_{name}` in pytest. |
| `api` | Yes | `"auth"`, `"preflight"`, or `"workflow"` |
| `assert_that` | Yes | Dict mapping dot-notation paths to predicates |
| `credentials` | No | Override auto-loaded creds. Use for negative tests only. |
| `metadata` | No | Per-scenario metadata override |
| `connection` | No | Per-scenario connection override |
| `description` | No | Human-readable description shown in output |
| `skip` | No | `True` to skip |
| `skip_reason` | No | Reason string shown when skipped |

**Key rule:** Only set `credentials` on a Scenario when you want to **override** the auto-loaded ones (e.g. negative tests). For positive tests, omit `credentials` entirely.

### Credential Auto-Discovery

The framework reads `ATLAN_APPLICATION_NAME`, then loads all `E2E_{APP_NAME}_*` env vars:
```
E2E_POSTGRES_USERNAME=postgres  →  {"username": "postgres"}
E2E_POSTGRES_HOST=localhost     →  {"host": "localhost"}
E2E_POSTGRES_PORT=5432          →  {"port": 5432}   ← auto-converted to int
```
These are merged with `default_credentials` (class-level). `default_credentials` wins on conflicts.

### API Response Shapes

**Auth** (`POST /workflows/v1/auth`):
```json
// Success
{"success": true, "message": "Authentication successful"}
// Failure
{"success": false, "error": "...", "details": "..."}
```

**Preflight** (`POST /workflows/v1/check`):
```json
// Success
{
  "success": true,
  "data": {
    "databaseSchemaCheck": {"success": true, "successMessage": "...", "failureMessage": ""},
    "tablesCheck": {"success": true, "successMessage": "Tables check successful. Table count: 42", "failureMessage": ""},
    "versionCheck": {"success": true, "successMessage": "...", "failureMessage": ""}
  }
}
// Failure
{"success": false, "error": "...", "details": "..."}
```

**Workflow** (`POST /workflows/v1/start`):
```json
// Success
{"success": true, "message": "Workflow started successfully", "data": {"workflow_id": "...", "run_id": "..."}}
```

### Assertion Reference

```python
# Basic
equals(True)           # exact equality
not_equals("error")    # inequality
exists()               # not None
is_none()              # is None
is_true()              # truthy
is_false()             # falsy

# Collections
one_of(["a", "b"])     # value in list
contains("Table count:")  # substring or item in collection
has_length(5)          # len == 5
is_empty()             # empty
is_not_empty()         # non-empty

# Numeric
greater_than(0)
between(0, 100)

# String
matches(r"^\d+\.\d+")  # regex
starts_with("http")
ends_with(".csv")

# Type
is_dict()
is_list()
is_string()
is_type(str)

# Combinators
all_of(is_string(), is_not_empty())
any_of(equals("ok"), equals("success"))
none_of(contains("error"))

# Custom
custom(lambda x: x % 2 == 0, "is_even")
```

### Nested Path Access
Use dot notation to traverse response dicts:
```python
"data.databaseSchemaCheck.success"  # → response["data"]["databaseSchemaCheck"]["success"]
"data.workflow_id"                   # → response["data"]["workflow_id"]
```

## Step 3: Generate Scenarios

Write scenarios covering all three tiers. Mark clearly with comments.

### Auth Scenarios (minimum 3, target 7+)

**Required:**
- `auth_valid_credentials` — valid creds succeed, message matches exactly
- `auth_response_structure` — response shape is correct (types)
- `auth_invalid_credentials` — completely wrong creds fail

**Recommended:**
- `auth_wrong_password` — correct user, wrong password only
- `auth_wrong_host` — unreachable/nonexistent host
- `auth_wrong_database` — valid server, nonexistent database
- `auth_wrong_port` — valid host, wrong port

**Connector-Specific** (add if relevant):
- SSL/TLS modes, IAM auth, OAuth, etc. — mark with `skip=True` if env might not support

**For negative tests**, provide a full credentials dict (all required fields) with just one field wrong:
```python
valid_creds_base = {
    "username": "{default_user}",
    "password": "{default_pass}",   # or a sensible default
    "host": "localhost",
    "port": {default_port},
    "database": "{test_db}",
    "authType": "basic",
    "type": "all",
}

Scenario(
    name="auth_wrong_password",
    api="auth",
    credentials={**valid_creds_base, "password": "definitely_wrong"},
    assert_that={"success": equals(False)},
    description="Correct user but wrong password fails",
),
```

### Preflight Scenarios (minimum 5, target 10+)

**Required:**
- `preflight_valid_configuration` — all three sub-checks pass, data is dict
- `preflight_database_schema_check` — databaseSchemaCheck passes
- `preflight_tables_check` — tablesCheck passes, message contains "Table count:"
- `preflight_version_check` — versionCheck passes
- `preflight_invalid_credentials` — fails with bad creds

**Recommended:**
- `preflight_nonexistent_database_in_filter` — filter refs a DB that doesn't exist → databaseSchemaCheck fails
- `preflight_nonexistent_schema_in_filter` — filter refs a schema that doesn't exist → databaseSchemaCheck fails
- `preflight_empty_include_filter` — `{}` include-filter still works
- `preflight_wildcard_schemas` — `"*"` for schemas works
- `preflight_multiple_schemas` — multiple schema patterns work

**Optional:**
- `preflight_exclude_filter` — exclude filter removes schemas
- `preflight_temp_table_regex` — temp table regex accepted
- `preflight_tables_check_count_nonzero` — count > 0
- `preflight_version_message_format` — message says "meets minimum"

### Workflow Scenarios (minimum 2, target 5+)

**Required:**
- `workflow_start_success` — all fields present, success
- `workflow_response_contains_ids` — IDs are strings

**Recommended:**
- `workflow_invalid_credentials` — fails with bad creds
- `workflow_custom_connection_name` — custom connection name accepted
- `workflow_narrow_filter` — narrow include-filter works

**Optional:**
- `workflow_wide_filter` — wildcard filter works
- `workflow_multiple_databases` — multi-db filter works

## Step 4: Write the File

### File Header

```python
"""Integration tests for {ConnectorName} connector.

Prerequisites:
    1. Set env vars in .env:
        ATLAN_APPLICATION_NAME={app_name}
        E2E_{APP_NAME}_USERNAME=...
        E2E_{APP_NAME}_PASSWORD=...
        E2E_{APP_NAME}_HOST=...
        E2E_{APP_NAME}_PORT=...
        E2E_{APP_NAME}_DATABASE=...

    2. Start services:
        uv run poe start-deps  # Dapr + Temporal
        uv run python main.py  # App server

    3. Run tests:
        uv run pytest tests/integration/ -v
        uv run pytest tests/integration/ -v -k "auth"
        uv run pytest tests/integration/ -v -k "preflight"
        uv run pytest tests/integration/ -v -k "workflow"
"""
```

### Negative Test Helper

Define `valid_creds_base` at module level (before the class) with sensible placeholder values. Use it for all negative tests that mutate a single field.

### Section Comments

Organize scenarios with comments:
```python
# =================================================================
# Auth Tests
# =================================================================
# ... auth scenarios ...

# =================================================================
# Preflight Tests
# =================================================================
# ... preflight scenarios ...

# =================================================================
# Workflow Tests
# =================================================================
# ... workflow scenarios ...
```

## Step 5: Create/Check `__init__.py`

If `tests/integration/__init__.py` doesn't exist, create it as an empty file.

## Running Tests

After generating, remind the user:

```bash
# Start dependencies (separate terminal)
uv run poe start-deps

# Start app server (separate terminal)
uv run python main.py

# Run all integration tests
uv run pytest tests/integration/ -v

# Run by API type
uv run pytest tests/integration/ -v -k "auth"
uv run pytest tests/integration/ -v -k "preflight"
uv run pytest tests/integration/ -v -k "workflow"

# Run a specific scenario
uv run pytest tests/integration/ -v -k "auth_valid_credentials"

# Show full output (print statements)
uv run pytest tests/integration/ -v -s
```

## Checklist Before Finishing

- [ ] `tests/integration/__init__.py` exists
- [ ] Test file has docstring with prerequisites
- [ ] `valid_creds_base` defined at module level if negative tests use it
- [ ] All required auth scenarios present (3+)
- [ ] All required preflight scenarios present (5+)
- [ ] All required workflow scenarios present (2+)
- [ ] Recommended scenarios added with connector-specific details
- [ ] Skipped scenarios have `skip_reason` set
- [ ] `default_credentials` has connector-specific static fields (authType, type, etc.)
- [ ] `default_metadata` uses a real database/schema from the `.env`
- [ ] `default_connection` uses the correct `app_name` in `connection_qualified_name`
- [ ] No hardcoded passwords or secrets in positive test scenarios (those use auto-discovery)
