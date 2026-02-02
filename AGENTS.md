# Repository Guidelines

Atlan Application SDK is a Python library for building applications on the Atlan platform.

Package manager: `uv` (task runner via `poe` in `pyproject.toml`).

Non-standard commands used across the repo:

- `uv sync --all-extras --all-groups` (full dev/test deps)
- `uv run poe start-deps` (local Dapr + Temporal)
- `uv run poe generate-apidocs` (MkDocs + API docs)
- `uv run pre-commit run --files <file>` (run pre-commit on specific file)
- `uv run pre-commit run --all-files` (run pre-commit on all files)

**IMPORTANT**: Always run pre-commit checks before committing. CI will fail if pre-commit checks fail.

**IMPORTANT**: For Dockerfile or dependency changes, run security scans (`trivy`, `grype`) before pushing. See `docs/build-scan-overview.md`.

**Commit Rules**:
- Never add Co-Authored-By lines to commits
- Follow Conventional Commits format from `.cursor/rules/commits.mdc`

Where to look next (progressive disclosure):

- `docs/agents/project-structure.md` — quick map of key directories.
- `docs/agents/dev-commands.md` — setup and repeatable workflows.
- `docs/agents/coding-standards.md` — formatting, logging, exceptions, performance.
- `docs/agents/testing.md` — test command, coverage config, test layout.
- `docs/agents/commits-prs.md` — commit format and PR expectations.
- `docs/agents/docs-updates.md` — which conceptual docs to update with code changes.
- `docs/agents/review.md` — code review checklist.
- `docs/agents/deepwiki.md` — DeepWiki MCP setup and when to verify SDK details.
- `docs/build-scan-overview.md` — image hierarchy, security scanning, Dapr setup.
