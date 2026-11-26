# Commits and PRs

- Commit message format is defined in `docs/standards/commits.md` (Conventional Commits).
- PR size and review expectations live in `CONTRIBUTING.md`.

## Before Committing

**Always run pre-commit checks before committing:**

```bash
uv run pre-commit run --files <changed-files>
```

CI will fail if pre-commit checks fail. Common failures:
- Linting errors (ruff)
- Formatting issues (ruff-format)
- Import order (ruff `I`)
- Type errors (pyright)

## Commit Message Format

Conventional Commits, `type(scope): description`. The allowed types and scopes are listed once, in `docs/standards/commits.md`; read them there rather than from a copy.

Example: `fix(io): align parquet writer row-group size with reader batch size`
