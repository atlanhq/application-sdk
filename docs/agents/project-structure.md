# Project Structure

- `application_sdk/` is the core SDK package. Packages with public exports (per the capability manifest): `app`, `clients`, `common`, `contracts`, `credentials`, `errors`, `execution`, `handler`, `infrastructure`, `main`, `observability`, `outputs`, `storage`, `templates`, `testing`. Internal packages with no public `__init__` exports: `decorators`, `docgen`, `server`, `tools`, `transformers`. Top-level modules: `constants.py`, `discovery.py`, `version.py`. Note: `test_utils/` is a legacy directory (only `test_utils/integration/` has content); decorators are co-located with their domain (`app/task.py`, `app/entrypoint.py`, `server/mcp/decorators.py`, `execution/decorators.py`) — use `application_sdk.testing` for test utilities.
- `tests/` contains the test suite, organized under `tests/unit/`, `tests/integration/`, `tests/e2e/`, and `tests/scratch/`.
- `docs/` holds documentation sources (guides, concepts, setup).
- `components/` stores Dapr/Temporal configs used by local dev tasks.
- `examples/` contains runnable sample applications.

For which docs to update when code changes, see `docs/agents/docs-updates.md`.
