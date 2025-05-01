from hypothesis import strategies as st

# Strategy for generating SQL credentials
sql_credentials_strategy = st.fixed_dictionaries(
    {
        "username": st.text(),
        "password": st.text(),
        "database": st.text(),
        "schema": st.text(),
        "warehouse": st.text().map(lambda x: x.upper() + "_WH"),
        "role": st.sampled_from(["ACCOUNTADMIN", "SYSADMIN", "USERADMIN", "PUBLIC"]),
    }
)

# Strategy for generating metadata SQL queries
metadata_sql_strategy = st.one_of(
    st.just("SELECT * FROM information_schema.tables"),
    st.just("SELECT * FROM information_schema.columns"),
    st.just("SELECT * FROM information_schema.views"),
    st.builds(
        lambda schema: f"SELECT * FROM information_schema.tables WHERE table_schema = '{schema}'",
        schema=st.text(),
    ),
)

# Strategy for generating metadata arguments
metadata_args_strategy = st.fixed_dictionaries(
    {
        "metadata_sql": metadata_sql_strategy,
        "database_alias_key": st.one_of(
            st.just("TABLE_CATALOG"), st.just("DATABASE_NAME"), st.just("CATALOG_NAME")
        ),
        "schema_alias_key": st.one_of(st.just("TABLE_SCHEMA"), st.just("SCHEMA_NAME")),
        "database_result_key": st.one_of(
            st.just("DATABASE"), st.just("CATALOG"), st.just("DB")
        ),
        "schema_result_key": st.one_of(st.just("SCHEMA"), st.just("NAMESPACE")),
    }
)

# Strategy for generating SQL query results
sql_column_strategy = st.fixed_dictionaries(
    {
        "name": st.text().map(str.upper),
        "type": st.sampled_from(
            ["VARCHAR", "INTEGER", "FLOAT", "TIMESTAMP", "BOOLEAN"]
        ),
        "nullable": st.booleans(),
    }
)

# Strategy for generating SQL query data
sql_data_strategy = st.lists(
    st.dictionaries(
        keys=st.text().map(str.upper),
        values=st.one_of(
            st.text(),
            st.integers(),
            st.floats(allow_infinity=False, allow_nan=False),
            st.booleans(),
            st.none(),
        ),
    ),
)

# Strategy for generating SQL queries
sql_query_strategy = st.one_of(
    st.just("SELECT * FROM test_table"),
    st.just("SELECT col1, col2 FROM test_table"),
    st.builds(lambda table: f"SELECT * FROM {table}", table=st.text()),
    st.builds(
        lambda table, limit: f"SELECT * FROM {table} LIMIT {limit}",
        table=st.text(),
        limit=st.integers(),
    ),
)

# Strategy for generating SQLAlchemy connection arguments
sqlalchemy_connect_args_strategy = st.fixed_dictionaries(
    {
        "connect_timeout": st.integers(),
        "retry_on_timeout": st.booleans(),
        "max_retries": st.integers(),
    }
)
