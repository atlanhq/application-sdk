"""Hypothesis strategies for the SQL metadata handler's query results.

.. deprecated:: 3.59.0
    Every strategy here is deprecated and removed in v4.0.0. Nothing in the
    SDK imports them and no test exercises them; build strategies from your
    app's own models in its tests instead.
"""

from hypothesis import strategies as st

from application_sdk import _deprecation

# Strategy for generating SQL query strings
_sql_query_strategy = st.text()

# Strategy for generating database names
_database_name_strategy = st.text()

# Strategy for generating schema names
_schema_name_strategy = st.text()

# Strategy for generating database entries (for database-only queries)
_database_entry_strategy = st.builds(
    lambda name: {"TABLE_CATALOG": name}, name=_database_name_strategy
)

# Strategy for generating schema entries (for schema-only queries)
_schema_entry_strategy = st.builds(
    lambda name: {"TABLE_SCHEMA": name}, name=_schema_name_strategy
)

# Strategy for generating full metadata entries (for combined database/schema queries)
_metadata_entry_strategy = st.builds(
    lambda db, schema: {"TABLE_CATALOG": db, "TABLE_SCHEMA": schema},
    db=_database_name_strategy,
    schema=_schema_name_strategy,
)

# Strategy for generating lists of database entries
_database_list_strategy = st.lists(
    _database_entry_strategy,
    unique_by=lambda x: x["TABLE_CATALOG"],
)

# Strategy for generating lists of schema entries
_schema_list_strategy = st.lists(
    _schema_entry_strategy,
    unique_by=lambda x: x["TABLE_SCHEMA"],
)

# Strategy for generating lists of metadata entries
_metadata_list_strategy = st.lists(
    _metadata_entry_strategy,
    unique_by=lambda x: (x["TABLE_CATALOG"], x["TABLE_SCHEMA"]),
)

# Strategy for generating SQL handler configuration
_sql_handler_config_strategy = st.just(
    {
        "metadata_sql": "SELECT * FROM test",
        "fetch_databases_sql": "SELECT database_name FROM databases",
        "fetch_schemas_sql": "SELECT schema_name FROM schemas WHERE database = '{database_name}'",
        "database_result_key": "TABLE_CATALOG",
        "schema_result_key": "TABLE_SCHEMA",
        "database_alias_key": "db_alias",
        "schema_alias_key": "schema_alias",
    }
)


#: name -> (replacement, why). Served by __getattr__ below (PEP 562).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "sql_query_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
    "database_name_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
    "schema_name_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
    "database_entry_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
    "schema_entry_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
    "metadata_entry_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
    "database_list_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
    "schema_list_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
    "metadata_list_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
    "sql_handler_config_strategy": (
        "a strategy built from your app's own metadata rows",
        "no SDK code or test uses this module",
    ),
}

#: A star import reads ``__all__`` and resolves each name through ``__getattr__``,
#: so ``import *`` keeps binding every old name, with the warning. Without it,
#: Python would enumerate only the module's real globals and skip them all.
__all__ = [*_DEPRECATED_CONSTANTS]


def _deprecated_constant_value(name: str) -> object:
    return globals()[f"_{name}"]


__getattr__ = _deprecation.deprecated_module_getattr(
    __name__,
    _DEPRECATED_CONSTANTS,
    remove_in="4.0.0",
    resolve=_deprecated_constant_value,
)
