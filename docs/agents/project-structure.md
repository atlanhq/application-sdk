# Project Structure

- `application_sdk/` is the core SDK package (`app`, `clients`, `common`, `contracts`, `credentials`, `decorators`, `docgen`, `execution`, `handler`, `infrastructure`, `observability`, `outputs`, `server`, `storage`, `templates`, `test_utils`, `testing`, `tools`, `transformers`, plus top-level `constants.py`, `discovery.py`, `errors.py`, `main.py`, `version.py`).
- `tests/` contains the test suite, organized under `tests/unit/`, `tests/integration/`, `tests/e2e/`, and `tests/scratch/`.
- `docs/` holds documentation sources (guides, concepts, setup).
- `components/` stores Dapr/Temporal configs used by local dev tasks.
- `examples/` contains runnable sample applications.

For which docs to update when code changes, see `docs/agents/docs-updates.md`.
