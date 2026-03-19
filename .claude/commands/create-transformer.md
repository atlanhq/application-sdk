# Create Transformer

Build a metadata transformer using Daft DataFrames.

## Source of Truth

Read these before starting:
- `docs/agents/coding-standards.md` — formatting, naming, type hints
- CLAUDE.md anti-pattern #8 — never use `to_pandas()`

## Reference Implementations

Study these for patterns:
- `application_sdk/transformers/__init__.py` — `TransformerInterface`, `transform_metadata()`

## Instructions

1. **Create class** inheriting from `TransformerInterface`
2. **Implement `transform_metadata()`**:
   ```python
   async def transform_metadata(
       self,
       typename: str,
       dataframe: daft.DataFrame,
       workflow_id: str,
       workflow_run_id: str,
       entity_class_definitions: Optional[dict] = None,
       **kwargs,
   ) -> daft.DataFrame:
       # Transform using Daft operations only
   ```
3. **Use `entity_class_definitions`** for type routing when handling multiple entity types
4. **Stay in Daft** — use `.with_column()`, `.filter()`, `.select()`, `.join()` — never `to_pandas()`
5. **Add type hints and docstrings**
6. **Run pre-commit**: `uv run pre-commit run --files <your-transformer-file>`
7. **Create tests** at `tests/unit/transformers/test_<name>.py` — create small Daft DataFrames as test fixtures

$ARGUMENTS
