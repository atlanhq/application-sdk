# E2E Examples Action

This GitHub Action runs End-to-End (E2E) tests for application-sdk examples. It executes a comprehensive suite of end-to-end tests by setting up the required infrastructure, installing dependencies, and running the examples in a production-like environment.

The action ensures that the examples provided in the SDK are working correctly and can serve as reliable reference implementations for SDK users.

## Inputs

| Input                  | Required | Description                              |
| ---------------------- | -------- | ---------------------------------------- |
| `github-token`         | Yes      | The GitHub token to use for the action   |
| `postgres-host`        | Yes      | The host of the Postgres database        |
| `postgres-password`    | Yes      | The password of the Postgres database    |
| `snowflake-account-id` | Yes      | The account ID of the Snowflake database |
| `snowflake-user`       | Yes      | The user of the Snowflake database       |
| `snowflake-password`   | Yes      | The password of the Snowflake database   |

## What it does

1. Sets up infrastructure:
    - Installs Dapr CLI (v1.14.1) with runtime v1.13.6
    - Installs and starts Temporal server
2. Sets up Python environment:
    - Installs Python 3.11
    - Installs Poetry 1.8.5
    - Installs project dependencies with all extras
3. Runs the examples:
    - Starts platform services
    - Executes example workflows
    - Captures and reports workflow status
4. Posts test results as a PR comment (if running on a PR)

## Usage Example

```yaml
name: Test Examples
on:
  pull_request:
    branches: [main]

jobs:
  test-examples:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run Example Tests
        uses: ./.github/actions/e2e-examples
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          postgres-host: ${{ secrets.POSTGRES_HOST }}
          postgres-password: ${{ secrets.POSTGRES_PASSWORD }}
          snowflake-account-id: ${{ secrets.SNOWFLAKE_ACCOUNT_ID }}
          snowflake-user: ${{ secrets.SNOWFLAKE_USER }}
          snowflake-password: ${{ secrets.SNOWFLAKE_PASSWORD }}
```

## Output

The action:

1. Creates a `workflow_status.md` file in the examples directory
2. Posts the workflow status as a comment on the PR (if running on a PR)
3. Fails if any example workflow fails
4. Succeeds only when all example workflows complete successfully