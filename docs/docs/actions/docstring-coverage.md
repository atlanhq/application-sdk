# Docstring Coverage Action

This GitHub Action reports Python docstring coverage in a specified Python module using interrogate. It analyzes Python files for missing docstrings in functions, classes, and modules, generates both summary and detailed coverage reports, and can fail the build if coverage falls below the specified threshold.

## Inputs

| Input         | Required | Default | Description                                    |
| ------------- | -------- | ------- | ---------------------------------------------- |
| `fail-under`  | No       | `80`    | Minimum required docstring coverage percentage |
| `module-name` | No       | `app`   | Target Python module for coverage check        |

## What it does

1. Sets up Python 3.11
2. Installs the `interrogate` package
3. Creates a coverage report with:
    - Summary of docstring coverage
    - Detailed report showing missing docstrings
4. Comments the coverage report on the PR with:
    - Basic coverage statistics
    - Detailed coverage report in a collapsible section

## Usage Example

```yaml
name: Check Docstring Coverage
on:
  pull_request:
    branches: [main]

jobs:
  docstring-coverage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Check Docstring Coverage
        uses: ./.github/actions/docstring-coverage
        with:
          fail-under: '90'
          module-name: 'my_package'
```

## Output

The action:

1. Creates a markdown file `docstring-cov.md` with the coverage report
2. Posts the report as a comment on the PR
3. Fails the build if coverage is below the specified threshold