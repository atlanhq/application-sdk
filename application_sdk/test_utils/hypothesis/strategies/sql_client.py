from hypothesis import strategies as st

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
        "pool_size": st.integers(),
        "max_overflow": st.integers(),
        "pool_timeout": st.integers(),
        "pool_recycle": st.integers(),
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
