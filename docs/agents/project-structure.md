# Project Structure

- `application_sdk/` is the core SDK package (`app`, `clients`, `common`, `contracts`, `credentials`, `docgen`, `execution`, `handler`, `infrastructure`, `observability`, `outputs`, `server`, `storage`, `templates`, `testing`, `tools`, `transformers`, plus top-level `constants.py`, `discovery.py`, `errors.py`, `main.py`, `version.py`). Note: `test_utils/` is a legacy directory (only `test_utils/integration/` has content); v3 decorators live in `app/` and `server/mcp/`, and test utilities live in `testing/`.
- `tests/` contains the test suite, organized under `tests/unit/`, `tests/integration/`, `tests/e2e/`, and `tests/scratch/`.
- `docs/` holds documentation sources (guides, concepts, setup).
- `components/` stores Dapr/Temporal configs used by local dev tasks.
- `examples/` contains runnable sample applications.

For which docs to update when code changes, see `docs/agents/docs-updates.md`.
