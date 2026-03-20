# migrate_v3 — v2 → v3 migration tooling

Three tools that together automate most of the work of migrating a connector
from application-sdk v2 to v3.

| Tool | What it does |
|------|-------------|
| `rewrite_imports.py` | Deterministic import-path rewriter (libcst) |
| `check_migration.py` | Static checker — definition of "done" |
| `MIGRATION_PROMPT.md` | AI agent instructions for structural refactoring |

---

## Recommended workflow

```
1. rewrite_imports          →  handles ~40 import path changes deterministically
2. check_migration          →  shows remaining structural work
3. AI / human               →  follows MIGRATION_PROMPT.md to merge classes
3b. directory consolidation →  move app/activities/<name>.py → app/<name>.py, delete v2 dirs
4. check_migration          →  all FAILs should now pass
5. pytest                   →  confirm nothing broke
5b. e2e test generation     →  generate v3 equivalent if BaseTest e2e tests exist
```

---

## Prerequisites

```bash
# libcst must be installed (it is in the [dev] dependency group)
uv sync --all-extras --all-groups
```

### Connector dependency on application-sdk

Until v3 is published to PyPI, the connector repo must install `atlan-application-sdk`
directly from the `refactor-v3` branch.  Add the following to the connector's
`pyproject.toml`:

```toml
[tool.uv.sources]
atlan-application-sdk = { git = "https://github.com/atlanhq/application-sdk", branch = "refactor-v3" }
```

Or run:

```bash
# inside the connector repo
uv add atlan-application-sdk --git https://github.com/atlanhq/application-sdk --branch refactor-v3
```

Once `atlan-application-sdk` v3 is released on PyPI, remove the `[tool.uv.sources]`
override and replace the dependency with a normal version pin
(`atlan-application-sdk>=3.0.0`).

---

## Step 1 — Rewrite imports

```bash
# Rewrite a single connector file in-place
python -m tools.migrate_v3.rewrite_imports path/to/connector.py

# Rewrite an entire connector repo directory
python -m tools.migrate_v3.rewrite_imports src/my_connector/

# Preview changes without writing (dry-run)
python -m tools.migrate_v3.rewrite_imports --dry-run src/my_connector/
```

The rewriter:
- Rewrites all deprecated `from application_sdk.<v2> import …` paths.
- Adds `# TODO(v3-migration): …` comments above imports that also require
  structural refactoring (class merges, API changes, removed symbols).
- Preserves all formatting, comments, and non-import code exactly.

Imports that the rewriter **cannot** fix automatically (structural work):

| v2 | v3 | Why manual |
|----|----|----|
| `WorkflowInterface` + `ActivitiesInterface` | `App` + `@task` | Classes must be merged |
| `BaseSQLMetadataExtractionWorkflow` + `Activities` | `SqlMetadataExtractor` | Classes must be merged |
| `BaseApplication` | `run_dev_combined()` | Different call pattern |
| `Worker` | `create_worker()` | Registration is automatic now |
| `auto_heartbeater` | built into `@task` | Decorator removed |

---

## Step 2 — Check what remains

```bash
# Check a directory
python -m tools.migrate_v3.check_migration src/my_connector/

# Check a single file
python -m tools.migrate_v3.check_migration src/my_connector/app.py

# No colour (for CI logs)
python -m tools.migrate_v3.check_migration --no-color src/
```

Exit codes:
- `0` — all FAIL checks pass (WARNs may be present)
- `1` — one or more FAIL checks remain
- `2` — usage error

### FAIL checks (block the migration)

| Rule | What it detects |
|------|-----------------|
| `no-deprecated-imports` | Any remaining `from application_sdk.<v2> import …` |
| `no-v2-decorators` | `@workflow.defn`, `@activity.defn`, `@auto_heartbeater` |
| `no-execute-activity-method` | `workflow.execute_activity_method(…)` |
| `no-sync-get-client` | Sync `get_client(` call |
| `handler-typed-signatures` | Handler methods still using `*args`/`**kwargs` instead of typed contracts |
| `no-unbounded-escape-hatch` | `allow_unbounded_fields=True` in connector contracts (SDK-internal only) |

### WARN checks (advisory)

| Rule | What it detects |
|------|-----------------|
| `typed-task-signatures` | `Dict[str, Any]` near a `@task` method |
| `app-subclass-missing` | No `App` / template subclass found in the tree |
| `handler-base` | Handler class not using v3 `Handler` base |
| `entry-point` | No `run_dev_combined` or CLI reference found |
| `no-v2-directory-structure` | `app/activities/` or `app/workflows/` directories still present |

---

## Step 3 — Structural refactoring

Open `MIGRATION_PROMPT.md` and follow the relevant section for your
connector type:

- §2a — SQL metadata extraction
- §2b — SQL query extraction
- §2c — Incremental SQL metadata extraction
- §3  — Custom (non-SQL) connector
- §4  — Handler (always required)
- §5  — Entry point (always required)

Run the checker again after finishing each section to track progress.

---

## Step 4 — Verify

```bash
# Re-run the checker — all FAILs should pass
python -m tools.migrate_v3.check_migration src/my_connector/

# Run the connector's test suite
uv run pytest tests/
```

---

## File layout

```
tools/migrate_v3/
  __init__.py           package marker
  import_mapping.py     single source of truth for all v2→v3 mappings
  rewrite_imports.py    libcst codemod (Category A — deterministic)
  check_migration.py    grep-based validation script (stdlib only)
  MIGRATION_PROMPT.md   AI agent instructions (Categories B+C)
  README.md             this file
```

---

## Adding a new mapping

If a connector uses a deprecated symbol that is not in the mapping table yet:

1. Open `import_mapping.py`.
2. Add an entry to `SYMBOL_MAP` (for a specific symbol rename) or `MODULE_MAP`
   (for a module-path-only change where the symbol name is unchanged).
3. Re-run the rewriter and checker to confirm the new entry behaves correctly.

---

## Caveats

- The rewriter only handles `from X import Y` style imports.
  `import application_sdk.workflows` (bare module imports without `from`) are
  not rewritten — these are uncommon in practice.
- The checker uses pattern matching, not AST analysis.  False positives are
  possible in strings or comments; use `# noqa` or rename the variable if
  needed.
- `Dict[str, Any]` warnings fire only in files that also contain `@task`.
