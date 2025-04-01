# Setup Dependencies Action

This GitHub Action sets up Python project dependencies and manages dependency caching for Atlan Apps. It handles the installation of Python 3.11, Poetry package manager, and project dependencies while implementing efficient caching strategies to speed up workflow execution.

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `cache` | No | `true` | Enable dependency caching |

## What it does

1. Sets up Python 3.11
2. Installs Poetry 1.8.5 with configuration:
    - Creates project-specific virtualenv
    - Sets virtualenv path to `.venv`
    - Enables parallel installation
3. Manages dependency caching:
    - Caches the virtual environment based on OS, Python version, and dependency files
    - Uses cache key: `venv-{os}-{python-version}-{hash(poetry.lock, pyproject.toml)}`
4. Installs dependencies if cache miss:
    - Installs all project dependencies with extras
    - Skips root project installation
    - Runs in non-interactive mode

## Usage Example

```yaml
name: Build and Test
on:
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup Dependencies
        uses: ./.github/actions/setup-deps
        with:
          cache: 'true'  # Enable caching (optional, defaults to true)

      # Your subsequent steps can now use the Python environment
      - name: Run Tests
        run: poetry run pytest
```

## Output

The action:

1. Creates a Python virtual environment in `.venv`
2. Installs all project dependencies
3. Caches the virtual environment for future runs
4. Makes the environment available for subsequent steps in the workflow