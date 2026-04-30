# Project Structure

- `application_sdk/` is the core SDK package (`app`, `clients`, `common`, `contracts`, `credentials`, `decorators`, `execution`, `handler`, `infrastructure`, `observability`, `outputs`, `server`, `storage`, `templates`, `testing`, `tools`, `transformers`, plus top-level `constants.py`, `errors.py`, `main.py`).
- `tests/` contains the test suite (currently organized under `tests/unit/`).
- `docs/` holds documentation sources (guides, concepts, setup).
- `components/` stores Dapr/Temporal configs used by local dev tasks.
- `examples/` contains runnable sample applications.

For which docs to update when code changes, see `docs/agents/docs-updates.md`.
