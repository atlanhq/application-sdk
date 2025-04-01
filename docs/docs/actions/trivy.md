# Trivy Code Scanner Action

This GitHub Action runs Trivy security scanner on the codebase to detect and report vulnerabilities. It scans the codebase and generates reports in multiple formats (SARIF and JSON) that can be used for security analysis and monitoring.

## Inputs

| Input                      | Required | Default | Description                                                    |
| -------------------------- | -------- | ------- | -------------------------------------------------------------- |
| `add-report-comment-to-pr` | No       | `true`  | Whether to add a comment to the PR with the Trivy scan results |

## What it does

1. Performs security scans:
    - Runs filesystem scan using Trivy v0.28.0
    - Generates SARIF report for GitHub Security tab
    - Creates JSON report for PR comments
    - Focuses on CRITICAL and HIGH severity issues
2. Uploads scan results:
    - Uploads SARIF file to GitHub Security tab
    - Converts JSON results to markdown (if PR comments enabled)
3. Handles PR integration:
    - Comments scan results on PR (if enabled)
    - Fails the workflow on HIGH/CRITICAL vulnerabilities

## Usage Example

```yaml
name: Security Scan
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run Security Scan
        uses: ./.github/actions/trivy
        with:
          add-report-comment-to-pr: 'true'  # Optional, defaults to true
```

## Output

The action produces:

1. `trivy-results.sarif`: Detailed scan results in SARIF format
2. `trivy-results.json`: Scan results in JSON format
3. `trivy-results.md`: Markdown report for PR comments (if enabled)
4. PR comment with vulnerability summary (if enabled)
5. Entries in GitHub Security tab showing detected vulnerabilities

## Environment Configuration

The action uses the following Trivy database configurations:

- TRIVY_DB_REPOSITORY: `public.ecr.aws/aquasecurity/trivy-db:2`
- TRIVY_JAVA_DB_REPOSITORY: `public.ecr.aws/aquasecurity/trivy-java-db:1`