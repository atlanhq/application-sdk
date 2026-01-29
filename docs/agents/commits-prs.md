# Commits and PRs

- Commit message format is defined in `.cursor/rules/commits.mdc` (Conventional Commits).
- PR size and review expectations live in `CONTRIBUTING.md`.

## Before Committing

**Always run pre-commit checks before committing:**

```bash
uv run pre-commit run --files <changed-files>
```

CI will fail if pre-commit checks fail. Common failures:
- Linting errors (ruff)
- Formatting issues (ruff-format)
- Import order (isort)
- Type errors (pyright)

## Commit Message Format

Use Conventional Commits format:
- `fix: description` - Bug fixes
- `feat: description` - New features
- `chore: description` - Maintenance tasks
- `docs: description` - Documentation changes
- `refactor: description` - Code refactoring
- `test: description` - Test additions/changes

Example: `fix(io): align daft writer behavior with pandas to prevent data loss`
