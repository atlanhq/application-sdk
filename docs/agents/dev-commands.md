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

## Testing

```bash
# Run all tests
uv run pytest

# Run specific test file with verbose output
uv run pytest tests/unit/io/test_file.py -v -s

# Run tests matching a pattern
uv run pytest -k "test_name_pattern" -v
```
