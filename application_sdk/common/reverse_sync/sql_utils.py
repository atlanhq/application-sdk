"""Shared SQL utilities for reverse sync — used by all source apps."""


def object_name_from_qn(
    qualified_name: str, quote_char: str = '"', skip_parts: int = 3
) -> str:
    """Convert Atlan qualified name to quoted SQL object name.

    "default/snowflake/123/DB/SCHEMA/TABLE" -> '"DB"."SCHEMA"."TABLE"'
    "default/databricks/456/cat/sch/tbl"    -> '`cat`.`sch`.`tbl`'

    Args:
        qualified_name: Full Atlan qualified name.
        quote_char: Source-specific quote character (" for Snowflake, ` for Databricks/BigQuery).
        skip_parts: Number of leading QN segments to strip (default 3: "default/connector/conn_id").
    """
    parts = qualified_name.split("/")
    if len(parts) <= skip_parts:
        return ""
    return ".".join(f"{quote_char}{p}{quote_char}" for p in parts[skip_parts:])


def escape_sql_value(value: str) -> str:
    """Escape single quotes for SQL string literals."""
    return value.replace("'", "''")


# Entity type -> SQL ALTER keyword (source-agnostic where possible)
_ENTITY_TYPE_MAP = {
    "Table": "Table",
    "View": "View",
    "MaterialisedView": "View",
    "Schema": "Schema",
    "Database": "Database",
    "SnowflakeStream": "Stream",
    "SnowflakePipe": "Pipe",
}


def convert_entity_type_to_sql(entity_type_name: str) -> str:
    """Convert Atlan entity type to SQL ALTER keyword."""
    return _ENTITY_TYPE_MAP.get(entity_type_name, entity_type_name)
