# Project Structure

- `application_sdk/` is the core SDK package (clients, handlers, activities, workflows).
- `contract-toolkit/` is the separately released Pkl package for authoring app contracts.
- `tests/` contains the test suite (currently organized under `tests/unit/`).
- `docs/` holds documentation sources (guides, concepts, setup).
- `components/` stores Dapr/Temporal configs used by local dev tasks.

For which docs to update when code changes, see `docs/agents/docs-updates.md`.
For the generated public SDK surface inventory, see `docs/agents/sdk-capabilities.md`.
