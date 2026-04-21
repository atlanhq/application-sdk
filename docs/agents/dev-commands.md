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

Local dev uses the same credential flow as production. Credentials are split
into non-sensitive config (stored in object storage) and sensitive secrets
(stored in local files). The workflow receives only a `credential_guid`
pointer and resolves the full credential at runtime via `DaprCredentialVault`.

### Option 1: Use the poe utility (recommended)

```bash
# From a JSON string
uv run poe provision-credentials --body '{
  "host": "your-database-host.example.com",
  "port": "5432",
  "authType": "basic",
  "username": "myuser",
  "password": "mypassword",
  "connectorConfigName": "postgres",
  "extra": {}
}'

# From a JSON file
uv run poe provision-credentials --from-file creds.json
```

The command prints the `credential_guid` to use in the `/start` call.

### Option 2: Call the endpoint directly

```bash
# Auto-generate a GUID
curl -X POST "http://localhost:8000/dev/local-vault" \
  -H "Content-Type: application/json" \
  -d '{
    "host": "your-database-host.example.com",
    "port": "5432",
    "authType": "basic",
    "username": "myuser",
    "password": "mypassword",
    "connectorConfigName": "postgres",
    "extra": {}
  }'

# Or provide your own GUID
curl -X POST "http://localhost:8000/dev/local-vault/my-test-guid" \
  -H "Content-Type: application/json" \
  -d '{ ... }'
```

The endpoint splits the body into sensitive fields (`username`, `password`,
`extra`, `url`, `driverProperties`, `sodaConnection`) written to
`./local/dapr/secrets/{guid}.json`, and non-sensitive fields written to
object storage. This is gated to `DEPLOYMENT_NAME == local`.

### Start the workflow

```bash
curl -X POST "http://localhost:8000/workflows/v1/start" \
  -H "Content-Type: application/json" \
  -d "{
    \"credential_guid\": \"<GUID_FROM_ABOVE>\",
    \"connection\": {
      \"connection_name\": \"test-connection\",
      \"connection_qualified_name\": \"default/postgres/$(date +%s)\"
    },
    \"include_filter\": \"\",
    \"exclude_filter\": \"\"
  }"
```

Do **not** send raw credentials (username, password) in the `/start` body.
They will be stripped and ignored. Secrets are resolved at runtime from
the local secret files (dev) or Dapr secret store (prod).

For Claude Code agents invoking apps locally: provision credentials via
`POST /dev/local-vault`, then POST `/start` with the returned guid.

## Testing

```bash
# Run all tests
uv run pytest

# Run specific test file with verbose output
uv run pytest tests/unit/io/test_file.py -v -s

# Run tests matching a pattern
uv run pytest -k "test_name_pattern" -v
```
