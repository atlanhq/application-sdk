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

### Step 2: Download Dapr components (one-time)

```bash
uv run poe download-components
```

### Step 3: Start Dapr + Temporal

```bash
uv run poe start-deps
```

### Step 4: Start the app

```bash
uv run python main.py
```

The app starts in combined mode (handler + worker). Handler listens on
port 8000. At this point the app is running but no credentials are loaded
yet — those are resolved lazily when a workflow runs.

### Step 5: Provision credentials

With the app running, provision your credentials. The utility sends
them to the app, which splits sensitive from non-sensitive internally
and stores them for runtime resolution.

```bash
uv run poe provision-credentials --body '{
  "host": "your-database-host.example.com",
  "port": "5432",
  "authType": "basic",
  "username": "myuser",
  "password": "mypassword",
  "connectorConfigName": "postgres",
  "extra": {
    "database": "mydb"
  }
}'
# Returns: {"credential_guid": "a1b2c3d4..."}
```

Or from a file:

```bash
uv run poe provision-credentials --from-file creds.json
```

### Step 6: Start the workflow

```bash
curl -X POST "http://localhost:8000/workflows/v1/start" \
  -H "Content-Type: application/json" \
  -d "{
    \"credential_guid\": \"<GUID_FROM_STEP_5>\",
    \"connection\": {
      \"connection_name\": \"test-connection\",
      \"connection_qualified_name\": \"default/postgres/$(date +%s)\"
    },
    \"include_filter\": \"\",
    \"exclude_filter\": \"\"
  }"
```

Do **not** send raw credentials in the `/start` body — they will be stripped.
Credentials are resolved at runtime using the `credential_guid`.

## Testing

```bash
# Run all tests
uv run pytest

# Run specific test file with verbose output
uv run pytest tests/unit/io/test_file.py -v -s

# Run tests matching a pattern
uv run pytest -k "test_name_pattern" -v
```
