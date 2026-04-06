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
from application_sdk.testing.integration import (
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
| `connection_config` | No | Additional connection configuration dict (v3). Merged into the request payload. |
| `checks_to_run` | No | List of check names to run for preflight (v3). If omitted, all checks run. E.g. `["database_schema", "tables"]`. |
| `preflight_timeout` | No | Timeout in seconds for preflight checks (v3). Overrides the default timeout. |
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

> **Note:** The framework automatically converts flat credential dicts to v3's `[{"key": k, "value": v}]` format before sending to the server. Developers write credentials as flat dicts (the same as v2). The conversion is handled internally by the client.

### API Response Shapes

**Auth** (`POST /workflows/v1/auth`):
```json
// Success
{"success": true, "message": "Authentication success", "data": {"status": "success", "message": "", "identities": [], "scopes": [], "expires_at": ""}}
// Failure
{"success": true, "message": "Authentication failed", "data": {"status": "failed", "message": "...", "identities": [], "scopes": []}}
```

**Preflight** (`POST /workflows/v1/check`):
```json
// Success
{
  "success": true,
  "message": "Preflight check ready",
  "data": {
    "status": "ready",
    "checks": [
      {"name": "database_schema", "passed": true, "message": "...", "duration_ms": 50.0},
      {"name": "tables", "passed": true, "message": "Tables check passed. Table count: 42", "duration_ms": 30.0},
      {"name": "version", "passed": true, "message": "...", "duration_ms": 10.0}
    ],
    "message": "",
    "total_duration_ms": 90.0
  }
}
// Failure
{"success": true, "message": "Preflight check not_ready", "data": {"status": "not_ready", "checks": [{"name": "connectivity", "passed": false, "message": "..."}]}}
```

**Workflow** (`POST /workflows/v1/start`):
```json
// Success
{"success": true, "message": "Workflow started successfully", "data": {"workflow_id": "...", "run_id": "..."}, "correlation_id": "..."}
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
Use dot notation to traverse response dicts. Numeric segments are treated as list indices:
```python
"data.status"              # → response["data"]["status"] (auth/preflight)
"data.checks.0.passed"     # → response["data"]["checks"][0]["passed"]
"data.checks.0.message"    # → response["data"]["checks"][0]["message"]
"data.workflow_id"         # → response["data"]["workflow_id"]
```

## Step 3: Generate Scenarios

Write scenarios covering all three tiers. Mark clearly with comments.

### Auth Scenarios (minimum 3, target 7+)

**Required:**
- `auth_valid_credentials` — valid creds succeed, `data.status` is `"success"`
- `auth_response_structure` — response shape is correct (types)
- `auth_invalid_credentials` — completely wrong creds fail, `data.status` is `"failed"`

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

# v3 auth: check data.status instead of just success
Scenario(
    name="auth_valid_credentials",
    api="auth",
    assert_that={"success": equals(True), "data.status": equals("success")},
    description="Valid credentials authenticate successfully",
),

Scenario(
    name="auth_wrong_password",
    api="auth",
    credentials={**valid_creds_base, "password": "definitely_wrong"},
    assert_that={"success": equals(True), "data.status": equals("failed")},
    description="Correct user but wrong password fails",
),
```

### Preflight Scenarios (minimum 5, target 10+)

In v3, preflight checks are returned as a flat array in `data.checks`, not as named objects. The check names (e.g. `"database_schema"`, `"tables"`, `"version"`) are determined by the handler implementation. Use `data.status` for overall pass/fail and index into `data.checks` for individual results.

**Required:**
- `preflight_valid_configuration` — overall status is ready, checks list is non-empty
- `preflight_checks_all_pass` — every check in the array has `passed: true`
- `preflight_tables_check` — at least one check message contains "Table count:"
- `preflight_response_structure` — response shape is correct (`data.status` exists, `data.checks` is a list)
- `preflight_invalid_credentials` — fails with bad creds, `data.status` is `"not_ready"`

**Example assertions:**
```python
# Valid config — overall status is ready
Scenario(
    name="preflight_valid_configuration",
    api="preflight",
    assert_that={"success": equals(True), "data.status": equals("ready")},
    description="Valid configuration passes all preflight checks",
),

# Check individual results
Scenario(
    name="preflight_checks_all_pass",
    api="preflight",
    assert_that={"data.checks": is_not_empty(), "data.status": equals("ready")},
    description="All preflight checks pass with valid config",
),

# Tables check message
Scenario(
    name="preflight_tables_check",
    api="preflight",
    assert_that={"success": equals(True), "data.status": equals("ready")},
    description="Tables check passes and message contains table count",
),

# Invalid creds
Scenario(
    name="preflight_invalid_credentials",
    api="preflight",
    credentials={**valid_creds_base, "password": "definitely_wrong"},
    assert_that={"success": equals(True), "data.status": equals("not_ready")},
    description="Invalid credentials cause preflight to fail",
),
```

**Recommended:**
- `preflight_nonexistent_database_in_filter` — filter refs a DB that doesn't exist, check fails
- `preflight_nonexistent_schema_in_filter` — filter refs a schema that doesn't exist, check fails
- `preflight_empty_include_filter` — `{}` include-filter still works
- `preflight_wildcard_schemas` — `"*"` for schemas works
- `preflight_multiple_schemas` — multiple schema patterns work

**Optional:**
- `preflight_exclude_filter` — exclude filter removes schemas
- `preflight_temp_table_regex` — temp table regex accepted
- `preflight_tables_check_count_nonzero` — at least one check message references a count > 0
- `preflight_total_duration` — `data.total_duration_ms` is a positive number

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

## CI/CD Deployment — Ready-to-Use Workflow Templates

After the tests pass locally, deploy them to CI. Below are complete, copy-paste workflow templates.

### Template 1: Standard Connector (Public Source — Postgres, Redshift, Snowflake)

```yaml
# .github/workflows/integration-tests.yaml
name: Integration Tests

on:
  pull_request:
    types: [labeled]
  workflow_dispatch:

jobs:
  integration-test:
    if: >-
      github.event_name == 'workflow_dispatch' ||
      github.event.label.name == 'int-test'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    concurrency:
      group: integration-test-${{ github.ref }}
      cancel-in-progress: true
    permissions:
      pull-requests: write
      contents: write
      statuses: write

    steps:
      - name: Checkout PR branch
        uses: actions/checkout@v4.0.0

      - name: Install Dapr CLI
        run: |
          DAPR_VERSION="1.16.2"
          wget -q https://github.com/dapr/cli/releases/download/v${DAPR_VERSION}/dapr_linux_amd64.tar.gz -O /tmp/dapr.tar.gz
          tar -xzf /tmp/dapr.tar.gz -C /tmp
          sudo mv /tmp/dapr /usr/local/bin/
          chmod +x /usr/local/bin/dapr
          dapr init --runtime-version ${DAPR_VERSION} --slim

      - name: Install Temporal CLI
        run: curl -sSf https://temporal.download/cli.sh | sh

      - name: Add Dapr and Temporal to PATH
        run: |
          echo "$HOME/.dapr/bin" >> $GITHUB_PATH
          echo "$HOME/.temporalio/bin" >> $GITHUB_PATH

      - name: Setup Python, uv, and dependencies
        uses: atlanhq/application-sdk/.github/actions/setup-deps@main

      - name: Download Dapr components
        run: uv run poe download-components

      - name: Start Dapr + Temporal
        run: |
          uv run poe start-deps
          sleep 5

      - name: Start app server
        env:
          ATLAN_LOCAL_DEVELOPMENT: "true"
          ATLAN_APPLICATION_NAME: {APP_NAME}  # <-- CHANGE THIS
        run: |
          uv run python main.py &
          echo "Waiting for app server on :8000..."
          for i in $(seq 1 60); do
            if curl -sf http://localhost:8000/server/health > /dev/null 2>&1; then
              echo "App server ready after ${i}s"
              break
            fi
            if [ "$i" -eq 60 ]; then
              echo "::error::App server failed to start within 60s"
              exit 1
            fi
            sleep 1
          done

      - name: Run integration tests
        id: tests
        env:
          # <-- CHANGE THESE to match your connector's secrets
          E2E_{APP_NAME}_HOST: ${{ secrets.{APP_NAME}_HOST }}
          E2E_{APP_NAME}_PORT: "5432"
          E2E_{APP_NAME}_USERNAME: ${{ secrets.{APP_NAME}_USERNAME }}
          E2E_{APP_NAME}_PASSWORD: ${{ secrets.{APP_NAME}_PASSWORD }}
          E2E_{APP_NAME}_DATABASE: "default"
          ATLAN_LOCAL_DEVELOPMENT: "true"
          ATLAN_APPLICATION_NAME: {APP_NAME}
        run: |
          mkdir -p results
          set +e
          uv run pytest tests/integration/ -v \
            --tb=short \
            --junit-xml=results/test-results.xml \
            2>&1 | tee results/test-output.txt
          TEST_EXIT_CODE=${PIPESTATUS[0]}
          set -e
          SUMMARY=$(grep -E "^(FAILED|ERROR|=)" results/test-output.txt | tail -1)
          echo "summary=$SUMMARY" >> "$GITHUB_OUTPUT"
          exit $TEST_EXIT_CODE

      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: integration-test-results
          path: results/
          retention-days: 14

      - name: Post PR comment
        if: always() && github.event_name == 'pull_request'
        uses: mshick/add-pr-comment@b8f338c590a895d50bcbfa6c5859251edc8952fc
        with:
          message-id: "integration_test_results"
          message: |
            ## Integration Test Results
            **Status:** ${{ steps.tests.outcome == 'success' && 'Passed' || 'Failed' }}
            **Summary:** `${{ steps.tests.outputs.summary || 'No summary available' }}`
            **Run:** [${{ github.run_id }}](${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }})
        continue-on-error: true

      - name: Set commit status
        if: always() && github.event_name == 'pull_request'
        uses: actions/github-script@v7
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          script: |
            const state = '${{ steps.tests.outcome }}' === 'success' ? 'success' : 'failure';
            await github.rest.repos.createCommitStatus({
              owner: context.repo.owner,
              repo: context.repo.repo,
              sha: context.payload.pull_request.head.sha,
              state: state,
              target_url: `${context.serverUrl}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}`,
              description: state === 'success' ? 'Integration tests passed' : 'Integration tests failed',
              context: 'integration-tests'
            });
        continue-on-error: true

      - name: Cleanup
        if: always()
        run: |
          kill $(lsof -t -i :8000) 2>/dev/null || true
          uv run poe stop-deps || true
```

### Template 2: VPN-Protected Source (ClickHouse, Oracle, on-prem)

Add these two steps **before** "Start Dapr + Temporal":

```yaml
      # Requires: GLOBALPROTECT_USERNAME, GLOBALPROTECT_PASSWORD (secrets)
      #           GLOBALPROTECT_PORTAL_URL (variable, e.g. vpn2.atlan.app)
      - name: Connect to VPN (GlobalProtect)
        uses: atlanhq/github-actions/globalprotect-connect-action@main
        with:
          portal-url: ${{ vars.GLOBALPROTECT_PORTAL_URL }}
          username: ${{ secrets.GLOBALPROTECT_USERNAME }}
          password: ${{ secrets.GLOBALPROTECT_PASSWORD }}

      - name: Verify VPN connectivity
        run: |
          echo "Testing source connectivity through VPN..."
          curl -sk --connect-timeout 10 https://{YOUR_SOURCE_HOST}:{PORT} \
            && echo "Source reachable!" \
            || echo "Warning: source not reachable — tests may fail"
```

### Template 3: REST/PAT Auth Connector (Tableau, Salesforce)

For connectors with camelCase credential fields, put them in `default_credentials` on the test class (env var auto-discovery lowercases everything):

```python
class TestTableauIntegration(BaseIntegrationTest):
    # CamelCase fields must be here, not in env vars
    default_credentials = {
        "authType": "personal_access_token",
        "protocol": "https",
        "defaultSite": os.environ.get("E2E_TABLEAU_DEFAULTSITE", ""),
    }
```

And in the workflow YAML, only set the simple fields as env vars:
```yaml
        env:
          E2E_TABLEAU_HOST: ${{ secrets.TABLEAU_HOST }}
          E2E_TABLEAU_PORT: "443"
          E2E_TABLEAU_USERNAME: ${{ secrets.TABLEAU_PAT_TOKEN_NAME }}
          E2E_TABLEAU_PASSWORD: ${{ secrets.TABLEAU_PAT_TOKEN_VALUE }}
          E2E_TABLEAU_DEFAULTSITE: ${{ secrets.TABLEAU_SITE }}
```

### GitHub Secrets Checklist

For each connector repo, add these secrets in Settings → Secrets → Actions:

| Secret | Example | Required |
|--------|---------|----------|
| `{APP}_HOST` | `my-db.rds.amazonaws.com` | Yes |
| `{APP}_PORT` | `5432` | Only if non-standard |
| `{APP}_USERNAME` | `admin` | Yes |
| `{APP}_PASSWORD` | `secret123` | Yes |
| `{APP}_DATABASE` | `default` | Only if needed |
| `GLOBALPROTECT_USERNAME` | `john.doe` | Only for VPN sources |
| `GLOBALPROTECT_PASSWORD` | (system password) | Only for VPN sources |

And one **variable** (Settings → Variables → Actions):

| Variable | Value | Required |
|----------|-------|----------|
| `GLOBALPROTECT_PORTAL_URL` | `vpn2.atlan.app` | Only for VPN sources |

### Reference Implementations

These are live, working pipelines you can copy from:

| Connector | Workflow File | Source Type | Demo PRs |
|-----------|--------------|-------------|----------|
| **Postgres** | [integration-tests.yaml](https://github.com/atlanhq/atlan-postgres-app/blob/demo/integration-tests-passing/.github/workflows/integration-tests.yaml) | SQL, public RDS | [#319](https://github.com/atlanhq/atlan-postgres-app/pull/319) (pass), [#320](https://github.com/atlanhq/atlan-postgres-app/pull/320) (fail) |
| **Tableau** | [integration-tests.yaml](https://github.com/atlanhq/atlan-tableau-app/blob/tests/integration-tests/.github/workflows/integration-tests.yaml) | REST, PAT auth | [#8](https://github.com/atlanhq/atlan-tableau-app/pull/8) (pass), [#9](https://github.com/atlanhq/atlan-tableau-app/pull/9) (fail) |
| **ClickHouse** | [integration-tests.yaml](https://github.com/atlanhq/atlan-clickhouse-app/blob/tests/integration-tests/.github/workflows/integration-tests.yaml) | SQL, VPN | [#28](https://github.com/atlanhq/atlan-clickhouse-app/pull/28) (pass), [#29](https://github.com/atlanhq/atlan-clickhouse-app/pull/29) (fail) |

## Enable Merge Blocking (CRITICAL — DO NOT SKIP)

**This step is mandatory.** Without it, the integration tests run but don't actually prevent broken code from being merged.

1. Go to the repo → **Settings** → **Branches** → **Add branch protection rule**
2. Branch name pattern: `main`
3. Check **"Require status checks to pass before merging"**
4. Search for `integration-tests` and select it
5. Click **Save changes**

**Why this matters:** With the new `publish.yaml` pipeline, merging to `main` automatically builds a container image and creates a release on the Global Marketplace. Without merge blocking, a broken PR goes straight from merge to production. The integration test status check is the gate that prevents this.

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

## IMPORTANT: After Everything Is Done — Prompt the User

After all tests pass and the CI workflow is deployed, you MUST ask the user:

---

**The integration tests are working and the CI workflow is deployed. There is one final critical step:**

**You need to enable branch protection so that failing integration tests actually block merging.**

Do you have admin access to this repo? If yes, go to:
> **Settings → Branches → Add branch protection rule**
> - Branch name pattern: `main`
> - Check "Require status checks to pass before merging"
> - Search for `integration-tests` and select it
> - Save

If you don't have admin access, ask your team lead or repo owner to do this. It takes 30 seconds.

**Without this step, the tests run but don't block anything.** Since `publish.yaml` auto-deploys on merge to main, broken code would go straight to production. This is the single most important configuration step.

Would you like me to help you verify the branch protection rule is set up correctly?

---

Do NOT skip this prompt. The entire value of the pipeline depends on merge blocking being enabled.
