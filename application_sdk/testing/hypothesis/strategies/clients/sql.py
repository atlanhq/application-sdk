"""Hypothesis strategies for SQL client tests.

The one home for SQL client strategies: the ones that used to live in
``application_sdk.testing.hypothesis.strategies.sql_client`` moved here, and
that module now serves them as deprecated aliases until v4.0.0.

Every integer is bounded. An unbounded ``st.integers()`` draws values no
driver or config accepts (negative timeouts, sizes beyond ``int64``), so a test
fails on the strategy instead of on the code under test.
"""

from hypothesis import strategies as st

#: The range a SQL ``BIGINT`` column holds; data values stay inside it.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

#: Upper bound for config durations, in seconds: one day.
_MAX_SECONDS = 86_400

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
        lambda schema: (
            f"SELECT * FROM information_schema.tables WHERE table_schema = '{schema}'"
        ),
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
            st.integers(min_value=_INT64_MIN, max_value=_INT64_MAX),
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
        limit=st.integers(min_value=0, max_value=_INT64_MAX),
    ),
)

# Strategy for generating SQLAlchemy connection arguments
sqlalchemy_connect_args_strategy = st.fixed_dictionaries(
    {
        "connect_timeout": st.integers(min_value=1, max_value=_MAX_SECONDS),
        "retry_on_timeout": st.booleans(),
        "max_retries": st.integers(min_value=0, max_value=100),
    }
)

# Strategy for generating mock SQL query results
mock_sql_row_strategy = st.fixed_dictionaries(
    {
        "col1": st.text(),
        "col2": st.text(),
    }
)

# Strategy for generating mock SQL column descriptions
mock_sql_column_description_strategy = st.fixed_dictionaries(
    {
        "name": st.text().map(str.upper),
    }
)

# Strategy for generating mock SQL query batches
mock_sql_batch_strategy = st.lists(
    mock_sql_row_strategy,
)

# Strategy for generating mock SQL query results with column descriptions
mock_sql_query_result_strategy = st.fixed_dictionaries(
    {
        "columns": st.lists(mock_sql_column_description_strategy),
        "batches": st.lists(mock_sql_batch_strategy),
    }
)

# Strategy for generating SQL engine configurations
sql_engine_config_strategy = st.fixed_dictionaries(
    {
        "pool_size": st.integers(min_value=1, max_value=100),
        "max_overflow": st.integers(min_value=0, max_value=100),
        "pool_timeout": st.integers(min_value=0, max_value=_MAX_SECONDS),
        # SQLAlchemy reads -1 as "never recycle".
        "pool_recycle": st.integers(min_value=-1, max_value=_MAX_SECONDS),
        "pool_pre_ping": st.booleans(),
    }
)

# Strategy for generating SQL connection strings
sql_connection_string_strategy = st.builds(
    lambda driver, host, port, db: f"{driver}://{host}:{port}/{db}",
    driver=st.sampled_from(["postgresql", "mysql", "sqlite", "oracle", "mssql"]),
    host=st.text(),
    port=st.integers(min_value=1, max_value=65535),
    db=st.text(),
)

# Strategy for generating SQL error scenarios
sql_error_strategy = st.sampled_from(
    [
        "OperationalError",
        "IntegrityError",
        "ProgrammingError",
        "DataError",
        "InternalError",
        "NotSupportedError",
        "TimeoutError",
    ]
)
