# Fix Pre-commit

Diagnose and fix pre-commit hook failures.

## Source of Truth

Read these before starting:
- `.pre-commit-config.yaml` — hook definitions and versions
- `docs/agents/coding-standards.md` — enforced standards

## Instructions

1. **Run all hooks**: `uv run pre-commit run --all-files`
2. **Fix by hook type**:
   - **ruff (lint)**: `uv run ruff check --fix <files>` — auto-fixes most issues
   - **ruff (format)**: `uv run ruff format <files>` — auto-formats code
   - **isort**: `uv run isort <files>` — fixes import ordering
   - **pyright**: Manual fixes required — add type annotations, fix type errors
   - **trailing-whitespace / end-of-file-fixer / mixed-line-ending**: Usually auto-fixed by pre-commit
3. **Re-run to verify**: `uv run pre-commit run --all-files`
4. **If hooks keep failing**: Read the specific error output carefully — some hooks (like pyright) require manual intervention
5. **Common pyright fixes**:
   - Add `-> None` return type to `__init__` methods
   - Use `Optional[X]` or `X | None` for nullable params
   - Import types from `typing` or `collections.abc`

$ARGUMENTS
