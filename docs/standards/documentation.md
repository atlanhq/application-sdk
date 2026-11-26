# Documentation Updates

When modifying SDK code, update corresponding conceptual documentation under `docs/concepts/` to reflect changes.

**Module to Concept Doc Mapping:**
*   `application_sdk/app/**` -> `docs/concepts/apps.md`
*   `application_sdk/templates/**` -> `docs/concepts/tasks.md`
*   `application_sdk/execution/**` -> `docs/concepts/apps.md`
*   `application_sdk/execution/progress*.py`, `application_sdk/execution/heartbeat.py` (watchdog paths) -> `docs/concepts/progress-and-stalls.md`
*   `application_sdk/handler/**` -> `docs/concepts/handlers.md`
*   `application_sdk/server/**` -> `docs/concepts/server.md`
*   `application_sdk/clients/**` -> `docs/concepts/clients.md`
*   `application_sdk/errors/**` -> `docs/concepts/common.md` (Error Handling section) and `docs/standards/exceptions.md`
*   `application_sdk/common/**` or `application_sdk/constants.py` -> `docs/concepts/common.md`
*   `application_sdk/transformers/**` -> `docs/concepts/common.md`
*   `application_sdk/infrastructure/**` -> `docs/concepts/apps.md` (infrastructure abstraction section)
*   `application_sdk/contracts/**` -> `docs/concepts/entry-points.md` (contract types section)
*   `application_sdk/credentials/**` -> `docs/concepts/handlers.md` (credential resolution section)
*   `application_sdk/storage/**` -> `docs/concepts/apps.md` (built-in storage tasks section)
*   `application_sdk/observability/**` -> `docs/concepts/monitoring.md`

## Python examples

Every `python` code block under `docs/` must parse, and its `application_sdk` imports must name modules and symbols that exist in the source tree. The `check-doc-examples` pre-commit hook (`.github/scripts/check_doc_examples.py`) enforces both. It runs on docs changes and on SDK changes, since renaming a symbol breaks the docs that import it.

*   Write fragments so they parse: put `...` in a comment rather than an argument list, give a signature a `: ...` body, and wrap a bare `except` or `finally` in a `try`.
*   A block that deliberately shows v2 code (the "before" side of a migration) goes on the line after `<!-- doc-example: v2 -->`. It must still parse; its imports are not resolved.
*   In a block showing v2 and v3 side by side, end the first line of each v2 import statement with a `# v2` comment instead.
