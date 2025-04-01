# E2E Apps Action

This GitHub Action runs End-to-End (E2E) tests for Atlan Apps. It executes a comprehensive suite of end-to-end tests by triggering E2E integration tests in the target app repository and monitoring their execution.

The action ensures that Atlan apps remain compatible with SDK updates and continue to function as expected in a production-like environment.

## Inputs

| Input          | Required | Description                            |
| -------------- | -------- | -------------------------------------- |
| `github-token` | Yes      | The GitHub token to use for the action |
| `repo-name`    | Yes      | The name of the app repository to test |

## What it does

1. Gets the application-sdk commit SHA
2. Triggers E2E integration tests in the target repository
3. Monitors test execution with:
    - Maximum workflow timeout of 120 seconds
    - Step retry interval of 10 seconds
    - Overall timeout of 180 seconds
4. Verifies successful test completion

## Usage Example

```yaml
name: Run E2E Tests
on:
  pull_request:
    branches: [main]

jobs:
  e2e-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run E2E Tests
        uses: ./.github/actions/e2e-apps
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          repo-name: 'my-atlan-app'
```

## Output

The action:
1. Outputs the test run ID and URL for monitoring
2. Continuously checks the workflow status
3. Fails if:
   - Tests don't complete within the timeout period
   - Tests complete but with a non-success conclusion
4. Succeeds only when tests complete successfully within the timeout period