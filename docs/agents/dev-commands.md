# Development Commands

- Setup and local run workflow: `docs/guides/getting-started.md` (uv sync, poe tasks, example run).
- Task runner and common scripts live in `pyproject.toml` under `[tool.poe.tasks]`.
- Docs build configuration is in `mkdocs.yml` (install with `uv sync --all-extras --all-groups`, then `uv run poe generate-apidocs`). Check a docs change with `uv run --group docs mkdocs build --strict`: the SDK Gate's Docs area runs exactly that and fails on a page missing from `nav`, a broken link or anchor, or a relative link out of `docs/`.
- Capability manifest: `uv run poe regen-capabilities` — refreshes `docs/agents/sdk-capabilities.md` from `application_sdk/` HEAD. CI surfaces drift on every PR; comment `/regen-manifest` on a PR to have a bot regenerate and push.

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
- **ruff**: Linting (E722 bare except, F541 useless f-strings, etc.) and import sorting (`I` rules; the hook runs with `--fix`)
- **ruff-format**: Code formatting (line length, spacing, etc.)

The dev group pins `ruff` to the hook's exact version, so `uv run ruff check --fix` and `uv run ruff format` give the same result as CI.
- **pyright**: Type checking, held to `pyright_baseline.json` (errors only; see "pyright on the SDK tree" in `docs/standards/ci.md`). It fails on a new error per file and rule, and on a fixed one until you run `uv run --frozen --all-extras --all-groups python .github/scripts/check_pyright_baseline.py --scope sdk --update` and commit the lower baseline.
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

### Step 3 (optional): Start an external Dapr + Temporal

`run_dev_combined()` in Step 4 always starts its own embedded Dapr runtime and in-process Temporal server, so skip this step for it. You need it only for the manual `application-sdk --mode combined` approach below, which expects a standalone Temporal server and Dapr sidecar, as production has. It needs the Temporal CLI but not the Dapr CLI; see the [Setup Overview](../standards/setup.md).

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

If you prefer to control each step, start Dapr + Temporal (Step 3), then start
the app without credentials and use curl:

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

This section is the one place the SDK's test commands are written down; other pages link here. Conventions for *writing* tests are in [Testing](testing.md) and the [testing standard](../standards/testing.md).

```bash
# Unit tests, the way a CI matrix leg runs them: parallel under xdist.
# addopts in pyproject.toml already deselects integration, e2e, load and perf.
uv run pytest --import-mode=importlib -n auto tests/

# The same with coverage, as the one coverage leg runs it (85% floor).
uv run pytest --import-mode=importlib -n auto --cov=application_sdk --cov-report=term tests/

# One file, or tests matching a pattern, serially with output shown
uv run pytest tests/unit/storage/test_file_ref_sync.py -v -s
uv run pytest -k "test_name_pattern" -v

# Slow unit tests (@pytest.mark.perf), excluded from the command above
uv run pytest tests/unit -m perf

# Integration tests: embedded Temporal and Dapr, nothing to start first
uv run poe test-integration

# CI's own driver scripts (.github/scripts/tests, its own pytest.ini)
uv run pytest .github/scripts/tests
```

Measure coverage with `--cov`, not `coverage run -m pytest`: `coverage run` traces only the parent process, so the xdist workers' coverage is lost. Leave out `--capture=no` and `--log-cli-level`: xdist ignores the first in its workers, and live logging cost 42% of the wall time. The exact per-leg command CI builds (socket guard, timeouts, durations) is `.github/scripts/unit_test_command.py`.
