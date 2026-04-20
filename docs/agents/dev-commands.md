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

## Starting a Workflow Locally (curl)

Local dev uses the same credential flow as production. Credentials are split
into non-sensitive config (stored in object storage) and sensitive secrets
(stored in Dapr secret store). The workflow receives only a `credential_guid`
pointer and resolves the full credential at runtime via `DaprCredentialVault`.

**Step 1: Write non-sensitive credential config to object storage**

```bash
CREDENTIAL_GUID="my-test-guid-$(uuidgen | tr '[:upper:]' '[:lower:]')"

curl -X POST "http://localhost:8000/workflows/v1/config/${CREDENTIAL_GUID}?type=credentials" \
  -H "Content-Type: application/json" \
  -d '{
    "host": "your-database-host.example.com",
    "port": "5432",
    "authType": "basic",
    "connectorConfigName": "postgres",
    "credentialSource": "direct"
  }'
```

**Step 2: Start the workflow with credential_guid + workflow params**

```bash
curl -X POST "http://localhost:8000/workflows/v1/start" \
  -H "Content-Type: application/json" \
  -d "{
    \"credential_guid\": \"${CREDENTIAL_GUID}\",
    \"connection\": {
      \"connection_name\": \"test-connection\",
      \"connection_qualified_name\": \"default/postgres/$(date +%s)\"
    },
    \"include_filter\": \"\",
    \"exclude_filter\": \"\"
  }"
```

Do **not** send raw credentials (username, password) in the `/start` body.
They will be stripped and ignored. Secrets must be provisioned in the Dapr
secret store (Kubernetes secrets in prod, local Dapr components in dev).

For Claude Code agents invoking apps locally: always use this two-step flow.
Generate a UUID for `credential_guid`, POST the non-sensitive config to
`/config`, then POST `/start` with the guid.

## Testing

```bash
# Run all tests
uv run pytest

# Run specific test file with verbose output
uv run pytest tests/unit/io/test_file.py -v -s

# Run tests matching a pattern
uv run pytest -k "test_name_pattern" -v
```
