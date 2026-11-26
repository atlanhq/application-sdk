# Documentation Updates

- When changing `application_sdk` modules, update the matching conceptual docs per `docs/standards/documentation.md`.
- Conceptual docs live under `docs/concepts/` (see the mapping in that rule file).
- A new page under `docs/` goes into `nav` in `mkdocs.yml`, and links to repo source use a full GitHub URL; CI's `mkdocs build --strict` fails otherwise (`docs/standards/documentation.md`).
- Agent guidance goes in `CLAUDE.md`, never `AGENTS.md`: each `AGENTS.md` is a generated pointer at the `CLAUDE.md` beside it, and the `check-agents-md-sync` pre-commit hook fails any other content (`.github/scripts/check_agents_md_sync.py --fix` regenerates it). A command or prerequisite is written once: commands in `docs/agents/dev-commands.md`, prerequisites in `docs/standards/setup.md`; other pages link to them.
- A new or changed env var gets a row in `docs/configuration.md`. `tests/unit/test_env_var_docs_coverage.py` fails until the name appears in a table row there (its own, or as a fallback in another row); a mention in prose does not count. See `docs/standards/env-vars.md`.

## Incremental state (`application_sdk/common/incremental/**`)

This package documents itself beside the code rather than under `docs/concepts/`. A change to it — in particular to `state/store.py` (`CurrentStateStore`, `RunStateDirs`), `state/state_writer.py`, `state/incremental_diff.py`, `marker.py` or `helpers.py` — updates:

- `application_sdk/common/incremental/README.md` — the module's architecture, layout and deprecation table.
- The skills under `application_sdk/common/incremental/skills/` that describe the changed behaviour: `implement-incremental-extraction/` (notably `references/state-management.md`), `incremental-migrate/` (notably `references/guardrails.md`), and `incremental-extraction.mdc`. Every symbol they name must exist; mark deprecated ones as such.
- `docs/standards/cross-repo-contracts.md` — the persistent-artifacts and current-state/incremental-diff entries, whenever the object-store layout, `.sdk-manifest`, run-stamped file names or `metadata.json` change. Other repos read these.
- `docs/agents/sdk-capabilities.md` — regenerate with `/capability-manifest` when a public symbol changes.
- `CHANGELOG.md` and the generated `docs/changelog/v*.md` archives — never: the release workflow writes them (see `docs/changelog/index.md`, which is maintained by hand).

The same applies to the template that drives the package, `application_sdk/templates/incremental_sql_metadata_extractor.py`, in addition to its `docs/concepts/tasks.md` mapping.
