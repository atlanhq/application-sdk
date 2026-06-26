# Transformers (Legacy)

> **Deprecation notice:** The `application_sdk.transformers` stack — `TransformerInterface`, `AtlasTransformer`, and `QueryBasedTransformer` — is a **legacy, back-compat-only** subsystem. New connectors should use the typed-record + pyatlan_v9 mapper pattern described in the [v3 migration guide](../upgrade-guide-v3.md#step-2-upgrade-sql-metadata-extraction). No public `__all__` is exposed; import from the submodules directly and treat them as internal.

## Background

The legacy transformer pipeline converts raw table data into Atlas entity JSONL via one of two strategies:

```
Raw SQL query results (list[dict] or pyarrow.Table)
        │
        ▼
  TransformerInterface.transform_metadata()  ← AtlasTransformer or QueryBasedTransformer
        │
        ▼
  Atlas entity JSONL
        │
        ▼
  App.upload() → atlan-objectstore (Atlan-owned)
```

This pipeline is used internally by `SqlMetadataExtractor.transform_data()`. If you subclass `SqlMetadataExtractor` today without overriding `transform_data()`, you are running through this legacy path.

## Migrating Away

For new connectors, and when upgrading existing ones, use the simpler pattern documented in the upgrade guide:

- [Step 2: Upgrade SQL Metadata Extraction](../upgrade-guide-v3.md#step-2-upgrade-sql-metadata-extraction) — replace `AtlasTransformer` with typed records → pure mapper functions → `pyatlan_v9` Asset instances
- Reference implementations: `atlan-openapi-app`, `atlan-azure-event-hub-app`

The import rewriter (`tools/migrate_v3/rewrite_imports.py`) will leave `# TODO(upgrade-v3)` markers on transformer code because it cannot auto-convert this pattern; apply the migration manually.

## Legacy Reference (back-compat only)

### `AtlasTransformer`

Converts metadata into Atlas entities using `pyatlan` model classes. Supports `typename` values: `DATABASE`, `SCHEMA`, `TABLE`, `VIEW`, `COLUMN`, `MATERIALIZED VIEW`, `PROCEDURE`, `FUNCTION`, `TAG_REF`.

```python
from application_sdk.transformers.atlas import AtlasTransformer  # deep import — legacy
```

Used inside `SqlMetadataExtractor.transform_data()`. To override the transformer, instantiate it inside your `transform_data()` override and wire it via the `TransformInput`/`TransformOutput` contracts:

```python
import os
from application_sdk.app import task
from application_sdk.templates.contracts import TransformInput, TransformOutput

class MyConnectorApp(SqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def transform_data(self, input: TransformInput) -> TransformOutput:
        transformer = AtlasTransformer(
            connector_name="my-connector",
            tenant_id=os.getenv("ATLAN_TENANT_ID", "default"),  # TransformInput has no tenant_id; read from env
        )
        # ... call transformer.transform_metadata() per typename as needed
        return TransformOutput(...)
```

### `QueryBasedTransformer`

A YAML-template-driven transformer. SQL queries defined in YAML files are executed against input data via DuckDB to produce transformed output. See the upgrade guide for why this approach is being retired.

```python
from application_sdk.transformers.query import QueryBasedTransformer  # deep import — legacy
```

### Engine

Both transformers use pyarrow and DuckDB internally (already included in the `sql` extra). No additional extras are needed beyond `[sql]`.

## See Also

- [v3 Migration Guide — Step 2](../upgrade-guide-v3.md#step-2-upgrade-sql-metadata-extraction) — the recommended migration path
- [Apps](apps.md) — `SqlMetadataExtractor` and the `transform_data` task contract
- [REST API Application Guide](../guides/rest-api-application-guide.md) — non-SQL connector without transformers
