# Unit Tests Action

This GitHub Action runs unit tests for the Atlan Application SDK. It executes a comprehensive suite of unit tests, generates coverage reports, and ensures code quality meets the required standards.

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `github-token` | Yes | - | The GitHub token to use for the action |
| `fail-under` | No | `60` | The minimum coverage threshold to enforce (percentage) |

## What it does

1. Test execution:
    - Runs pytest on all tests in the `tests/` directory
    - Uses hypothesis for property-based testing
    - Shows detailed test statistics
2. Coverage reporting:
    - Generates coverage reports in XML and HTML formats
    - Enforces minimum coverage threshold
    - Creates coverage report comments on PRs
3. Report publishing:
    - Uploads HTML coverage report to S3
    - Provides accessible URL for coverage report
    - Comments report URL on PR

## Usage Example

```yaml
name: Run Tests
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run Unit Tests
        uses: ./.github/actions/unit-tests
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          fail-under: '75'  # Optional: set higher coverage threshold
```

## Output

The action produces:

1. Test execution results with full trace and hypothesis statistics
2. Coverage reports:
    - `coverage.xml`: XML format for tooling integration
    - `htmlcov/`: HTML format for human-readable reports
3. PR comments:
    - Coverage percentage and changes
    - Link to detailed coverage report
4. Published reports:
    - Available at: `https://k.atlan.dev/coverage/application-sdk/{branch-name}`

## Requirements

- Python environment with poetry (setup using setup-deps action)
- Appropriate GitHub token permissions
- Access to S3 for report publishing
- Tests must be located in the `tests/` directory