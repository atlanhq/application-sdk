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

**Path Semantics (ObjectStore)**:
- Key and prefix params for ObjectStore APIs accept either `./local/tmp/...` workflow paths or object-store keys like `artifacts/...`; the SDK normalizes these internally.
- Local file params remain local paths: upload `source` and download `destination` are not treated as object-store keys.

**Critical Anti-Patterns** (never introduce these):

1. **Bare `except:`** — Always catch specific exceptions.
   ```python
   # Wrong
   try: result = await client.fetch()
   except: pass

   # Right
   try: result = await client.fetch()
   except httpx.HTTPStatusError as e: logger.error("Fetch failed", status=e.response.status_code)
   ```

2. **Blocking calls in async** — Use `httpx`/`aiohttp`, not `requests`; `asyncio.sleep()`, not `time.sleep()`.
   ```python
   # Wrong
   import requests
   resp = requests.get(url)

   # Right
   async with httpx.AsyncClient() as client:
       resp = await client.get(url)
   ```

3. **Missing type hints** — All function params and returns must have type annotations.
   ```python
   # Wrong
   def process(data, config): ...

   # Right
   def process(data: dict[str, Any], config: WorkflowConfig) -> ActivityResult: ...
   ```

4. **Deep nesting >3 levels** — Use early returns to flatten logic.
   ```python
   # Wrong
   if a:
       if b:
           if c:
               do_thing()

   # Right
   if not a: return
   if not b: return
   if not c: return
   do_thing()
   ```

5. **N+1 queries** — Batch database operations instead of looping.
   ```python
   # Wrong
   for id in ids:
       row = await db.fetch_one(id)

   # Right
   rows = await db.fetch_many(ids)
   ```

6. **String concatenation in loops** — Use `"".join()`.
   ```python
   # Wrong
   result = ""
   for s in strings: result += s

   # Right
   result = "".join(strings)
   ```

7. **Mutable default arguments** — Use `None` with inner init.
   ```python
   # Wrong
   def process(items: list[str] = []): ...

   # Right
   def process(items: list[str] | None = None):
       items = items or []
   ```

8. **`to_pandas()`** — Stay in Daft DataFrames throughout the pipeline.
   ```python
   # Wrong
   pdf = daft_df.to_pandas()
   pdf["col"] = pdf["col"].apply(fn)

   # Right
   daft_df = daft_df.with_column("col", daft_df["col"].apply(fn, return_dtype=DataType.string()))
   ```

9. **Missing `@auto_heartbeater`** — All `@activity.defn` methods that do I/O must use `@auto_heartbeater`.
   ```python
   # Wrong
   @activity.defn
   async def fetch_data(self) -> ActivityResult: ...

   # Right
   @activity.defn
   @auto_heartbeater
   async def fetch_data(self) -> ActivityResult: ...
   ```

10. **Sensitive data in logs** — Never log credentials, tokens, or connection strings.
    ```python
    # Wrong
    logger.info("Connecting", token=creds.api_key)

    # Right
    logger.info("Connecting", credential_guid=creds.guid)
    ```

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
