# Transformers

Transformers convert raw extraction data into the format Atlan's ingestion API expects. They are used internally by the `SqlMetadataExtractor` template — most connector developers do not call them directly.

## Overview

```
Raw SQL query results
        │
        ▼
  TransformerInterface.transform_metadata()
        │
        ▼
  Atlas entity JSONL
        │
        ▼
  upload_to_atlan() → Atlan ingestion API
```

The `TransformerInterface` base class defines the contract:

```python
from application_sdk.transformers import TransformerInterface

class TransformerInterface(ABC):
    def transform_metadata(
        self,
        typename: str,
        dataframe: "daft.DataFrame",
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: dict[str, type] | None = None,
        **kwargs,
    ) -> "daft.DataFrame": ...
```

All transformers operate on [Daft](https://www.getdaft.io/) DataFrames — a columnar data engine optimized for large-scale metadata transformation.

## Built-In Transformers

### `AtlasTransformer`

Converts metadata into Atlas entities (the native Atlan format) using `pyatlan` model classes.

```python
from application_sdk.transformers.atlas import AtlasTransformer
```

Supports these `typename` values out of the box: `DATABASE`, `SCHEMA`, `TABLE`, `COLUMN`, `PROCEDURE`, `FUNCTION`, and tag attachments.

This transformer is used by `SqlMetadataExtractor`. If you subclass `SqlMetadataExtractor`, you can swap it out:

```python
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.transformers.atlas import AtlasTransformer

class MyConnectorApp(SqlMetadataExtractor):
    transformer_class = AtlasTransformer   # default; override to customize
```

### `QueryBasedTransformer`

A YAML-template-driven transformer. SQL queries defined in YAML files are executed against raw Daft DataFrames to produce transformed output. Supports the same typenames as `AtlasTransformer`.

```python
from application_sdk.transformers.query import QueryBasedTransformer
```

The YAML templates map each typename to a set of column expressions:

```yaml
# Example: TABLE.yaml
columns:
  - name: attributes.name
    source_query: table_name
  - name: attributes.qualifiedName
    source_query: "concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', table_name)"
    source_columns: [connection_qualified_name, table_catalog, table_schema, table_name]
  - name: typeName
    source_query: "'Table'"
```

The transformer generates a SQL `SELECT` statement from the YAML, executes it via Daft, and restructures flat dot-notation column names into nested structs.

### `common/utils.py`

Shared utility functions used by both transformers:

- `process_text(text, max_length)` — truncates and strips HTML
- `build_atlas_qualified_name(...)` — constructs Atlas-style qualified names
- `build_phoenix_uri(...)` — URI builder for Phoenix entity references

## When to Use Transformers Directly

**You typically don't.** If you subclass `SqlMetadataExtractor`, the transformer is invoked automatically for each batch of extracted data.

Write your own transformer only if:
- You have a non-SQL source with a custom entity schema that does not map to `DATABASE/SCHEMA/TABLE/COLUMN`
- You need transformation logic not expressible in YAML column mappings

**Custom transformer example:**

```python
from application_sdk.transformers import TransformerInterface
import daft

class MyTransformer(TransformerInterface):
    def transform_metadata(
        self,
        typename: str,
        dataframe: daft.DataFrame,
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions=None,
        **kwargs,
    ) -> daft.DataFrame:
        if typename.upper() == "DASHBOARD":
            return dataframe.select(
                daft.col("id").alias("attributes.qualifiedName"),
                daft.col("name").alias("attributes.name"),
                daft.lit("Dashboard").alias("typeName"),
            )
        raise ValueError(f"Unsupported typename: {typename}")
```

Wire it into `SqlMetadataExtractor`:

```python
class MyConnectorApp(SqlMetadataExtractor):
    transformer_class = MyTransformer
```

## Daft Dependency

Daft is an optional dependency. It is pulled in by the `sql` extra:

```bash
uv add "atlan-application-sdk[sql]"
```

Transformers import Daft lazily at call time (`import daft` inside methods) so the package loads without Daft installed — you'll only get an `ImportError` if you actually invoke a transformer without the extra.

## See Also

- [Apps](apps.md) — `SqlMetadataExtractor` and the `transformer_class` attribute
- [Creating an SQL Application](../guides/sql-application-guide.md) — end-to-end SQL connector guide
- [Building a REST API Connector](../guides/rest-api-application-guide.md) — non-SQL connector without transformers
