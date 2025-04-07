from hypothesis import strategies as st

# Strategy for generating SQL query strings
sql_query_strategy = st.text()

# Strategy for generating database names
database_name_strategy = st.text()

# Strategy for generating schema names
schema_name_strategy = st.text()

# Strategy for generating database entries (for database-only queries)
database_entry_strategy = st.builds(
    lambda name: {"TABLE_CATALOG": name}, name=database_name_strategy
)

# Strategy for generating schema entries (for schema-only queries)
schema_entry_strategy = st.builds(
    lambda name: {"TABLE_SCHEMA": name}, name=schema_name_strategy
)

# Strategy for generating full metadata entries (for combined database/schema queries)
metadata_entry_strategy = st.builds(
    lambda db, schema: {"TABLE_CATALOG": db, "TABLE_SCHEMA": schema},
    db=database_name_strategy,
    schema=schema_name_strategy,
)

# Strategy for generating lists of database entries
database_list_strategy = st.lists(
    database_entry_strategy,
    unique_by=lambda x: x["TABLE_CATALOG"],
)

# Strategy for generating lists of schema entries
schema_list_strategy = st.lists(
    schema_entry_strategy,
    unique_by=lambda x: x["TABLE_SCHEMA"],
)

# Strategy for generating lists of metadata entries
metadata_list_strategy = st.lists(
    metadata_entry_strategy,
    unique_by=lambda x: (x["TABLE_CATALOG"], x["TABLE_SCHEMA"]),
)

# Strategy for generating SQL handler configuration
sql_handler_config_strategy = st.just(
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
