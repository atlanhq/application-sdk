---
name: transformers
description: Build metadata transformers using Daft DataFrames
user-invocable: true
---

# Build a Transformer

## Process

1. Read `application_sdk/transformers/__init__.py` for the `TransformerInterface` contract
2. Create a transformer class inheriting from `TransformerInterface`
3. Implement `transform_metadata()`:
   - Receives `typename`, `dataframe` (Daft), `workflow_id`, `workflow_run_id`, optional `entity_class_definitions`
   - Use `entity_class_definitions` for routing logic when handling multiple entity types
   - Return a transformed Daft DataFrame
4. Use only Daft operations: `.with_column()`, `.filter()`, `.select()`, `.join()`, `.explode()`, `.agg()`
5. **Never use `to_pandas()`** — this is a critical anti-pattern (see CLAUDE.md #8)
6. Run pre-commit and create tests with small Daft DataFrames as fixtures

## Key References

- `application_sdk/transformers/__init__.py` — `TransformerInterface`, method signature

## Handling `$ARGUMENTS`

If arguments specify entity types or transformation logic, use them to structure the type routing and column transformations.
