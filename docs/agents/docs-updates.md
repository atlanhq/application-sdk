# Documentation Updates

- When changing `application_sdk` modules, update the matching conceptual docs per `docs/standards/documentation.md`.
- Conceptual docs live under `docs/concepts/` (see the mapping in that rule file).

## Incremental state (`application_sdk/common/incremental/**`)

This package documents itself beside the code rather than under `docs/concepts/`. A change to it — in particular to `state/store.py` (`CurrentStateStore`, `RunStateDirs`), `state/state_writer.py`, `state/incremental_diff.py`, `marker.py` or `helpers.py` — updates:

- `application_sdk/common/incremental/README.md` — the module's architecture, layout and deprecation table.
- The skills under `application_sdk/common/incremental/skills/` that describe the changed behaviour: `implement-incremental-extraction/` (notably `references/state-management.md`), `incremental-migrate/` (notably `references/guardrails.md`), and `incremental-extraction.mdc`. Every symbol they name must exist; mark deprecated ones as such.
- `docs/standards/cross-repo-contracts.md` — the persistent-artifacts and current-state/incremental-diff entries, whenever the object-store layout, `.sdk-manifest`, run-stamped file names or `metadata.json` change. Other repos read these.
- `docs/agents/sdk-capabilities.md` — regenerate with `/capability-manifest` when a public symbol changes.

The same applies to the template that drives the package, `application_sdk/templates/incremental_sql_metadata_extractor.py`, in addition to its `docs/concepts/tasks.md` mapping.
