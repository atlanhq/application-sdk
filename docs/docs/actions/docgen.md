# Docgen Action

This GitHub Action automatically generates comprehensive documentation from Atlan docs manifest using the Atlan Apps SDK. It scans the specified source directory for markdown files, docstrings, type hints, and other documentation markers, then compiles them into structured documentation.

## Inputs

| Input          | Required | Description                            |
| -------------- | -------- | -------------------------------------- |
| `github-token` | Yes      | The GitHub token to use for the action |

## What it does

1. Sets up Python 3.11
2. Installs Poetry 1.8.5
3. Installs project dependencies along with mkdocs and mkdocs-material
4. Runs the documentation generation script that:
    - Verifies the documentation structure
    - Exports the documentation to the `dist` directory

## Usage Example

```yaml
name: Generate Documentation
on:
  push:
    branches: [main]

jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Generate Docs
        uses: ./.github/actions/docgen
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

## Output

The action generates documentation in the `dist` directory using the following configuration:
- Source documentation directory: `docs`
- Export directory: `dist`