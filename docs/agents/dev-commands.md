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

## Starting a Workflow Locally

### Step 1: Provision credentials

Give the utility your full credentials as JSON — it handles the rest and
returns a `credential_guid` to use when starting the workflow.

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

### Step 2: Start the workflow

```bash
curl -X POST "http://localhost:8000/workflows/v1/start" \
  -H "Content-Type: application/json" \
  -d "{
    \"credential_guid\": \"<GUID_FROM_STEP_1>\",
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
