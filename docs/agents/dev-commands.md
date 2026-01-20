# Development Commands

- Setup and local run workflow: `docs/guides/getting-started.md` (uv sync, poe tasks, example run).
- Task runner and common scripts live in `pyproject.toml` under `[tool.poe.tasks]`.
- Docs build configuration is in `mkdocs.yml` (install with `uv sync --group docs`, then `uv run poe generate-apidocs`).
