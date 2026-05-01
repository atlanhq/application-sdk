# Development Commands

- Setup and local run workflow: `docs/guides/getting-started.md` (uv sync, poe tasks, example run).
- Task runner and common scripts live in `pyproject.toml` under `[tool.poe.tasks]`.
- Docs build configuration is in `mkdocs.yml` (install with `uv sync --group docs`, then `uv run poe generate-apidocs`).

## Pre-commit Checks

**Always run pre-commit before committing code.** CI will fail if pre-commit checks fail.

```bash
# Run on specific file(s)
uv run pre-commit run --files path/to/file.py

# Run on all files
uv run pre-commit run --all-files

# Install pre-commit hooks (runs automatically on git commit)
uv run pre-commit install
```

### What pre-commit checks:
- **ruff**: Linting (E722 bare except, F541 useless f-strings, etc.)
- **ruff-format**: Code formatting (line length, spacing, etc.)
- **isort**: Import sorting (profile: black)
- **pyright**: Type checking
- **conventional-pre-commit**: Commit message format (on commit-msg stage)
- **pre-commit-hooks**: Merge conflicts, debug statements, trailing whitespace, BOM

### Common issues:
- `E722`: Use `except Exception:` instead of bare `except:`
- `F541`: Remove `f` prefix from strings without placeholders
- Unicode characters (emojis like `✓`, `❌`) fail on Windows - use ASCII alternatives

## Running Locally

### Step 1: Install dependencies

```bash
uv sync
```

### Step 2: Verify Dapr components are present

The `components/*.yaml` files are committed to the repo and do not need downloading. Confirm they exist:

```bash
ls components/
```

### Step 3: Start Dapr + Temporal

```bash
uv run poe start-deps
```

To provision credentials into the local vault without running the full app (useful for standalone handler tests):

```bash
uv run poe provision-credentials
```

### Step 4: Run the app with `run_dev_combined`

Create a `run_dev.py` script (or add to your existing one):

```python
import asyncio
from application_sdk.main import run_dev_combined
from app.my_app import MyApp

asyncio.run(
    run_dev_combined(
        MyApp,
        credentials={
            "host": "your-database-host.example.com",
            "port": "5432",
            "authType": "basic",
            "username": "myuser",
            "password": "mypassword",
            "extra": {"database": "mydb"},
        },
        example_input={
            "connection": {
                "connection_name": "test-connection",
                "connection_qualified_name": "default/my-app/1234",
            },
        },
    )
)
```

Then run:

```bash
uv run python run_dev.py
```

This does everything automatically:
1. Starts the handler + worker on port 8000
2. Provisions credentials (splits sensitive/non-sensitive, same as prod)
3. Starts the workflow with the provisioned `credential_guid`

### Manual curl approach

If you prefer to control each step, start the app without credentials
and use curl:

```bash
# Start the app
uv run application-sdk --mode combined --app app.connector:MyApp

# Provision credentials (app must be running)
curl -s -X POST http://localhost:8000/workflows/v1/dev/local-vault \
  -H "Content-Type: application/json" \
  -d '{"host": "localhost", "port": "5432", "authType": "basic",
       "username": "myuser", "password": "mypassword",
       "extra": {"database": "mydb"}}'
# Returns: {"data": {"credential_guid": "..."}, "success": true, "message": "Credentials provisioned successfully"}

# Start the workflow
curl -s -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{"credential_guid": "<GUID_FROM_ABOVE>",
       "connection": {"connection_name": "test", "connection_qualified_name": "default/app/1234"}}'
```

## Testing

```bash
# Run all tests
uv run pytest

# Run specific test file with verbose output
uv run pytest tests/unit/storage/test_file_ref_sync.py -v -s

# Run tests matching a pattern
uv run pytest -k "test_name_pattern" -v
```
