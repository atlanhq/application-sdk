# Trivy Container Scanner Action

This GitHub Action runs Trivy security scanner on container images to detect and report vulnerabilities. It builds the container image from the Dockerfile, scans it for security issues, and uploads the results to GitHub's Security tab.

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `skip-files` | No | - | Comma-separated list of files or directories to be skipped |
| `github-token` | Yes | - | The GitHub token to use for the action |

## What it does

1. Container image handling:
    - Builds Docker image from project's Dockerfile
    - Uses provided GitHub token for private repository access
    - Tags image as 'docker-trivy-tag:latest'
2. Security scanning:
    - Runs Trivy v0.29.0 on the built container
    - Ignores unfixed vulnerabilities
    - Focuses on CRITICAL and HIGH severity issues
    - Generates results in SARIF format
3. Results processing:
    - Uploads SARIF report to GitHub Security tab
    - Skips specified files/directories if provided

## Usage Example

```yaml
name: Container Security Scan
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

      - name: Scan Container
        uses: ./.github/actions/trivy-container
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          skip-files: 'tests/,docs/'  # Optional: skip directories
```

## Output

The action produces:

1. `trivy-results.sarif`: Detailed scan results in SARIF format
2. Entries in GitHub Security tab showing detected vulnerabilities
3. Local Docker image tagged as 'docker-trivy-tag:latest'

## Environment Configuration

The action uses the following Trivy database configurations:

- TRIVY_DB_REPOSITORY: `public.ecr.aws/aquasecurity/trivy-db:2`
- TRIVY_JAVA_DB_REPOSITORY: `public.ecr.aws/aquasecurity/trivy-java-db:1`

## Requirements

- A valid `Dockerfile` must exist in the repository root
- The Dockerfile must be able to build successfully
- Appropriate GitHub token permissions for accessing private repositories (if needed)