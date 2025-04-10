"""
SQL query constants used for metadata extraction from database systems.
These queries are organized by database operation type and are used by various SQL clients.
"""

# PostgreSQL Database Metadata Queries
FETCH_DATABASE_SQL = """
SELECT datname as database_name FROM pg_database WHERE datname = current_database();
"""

FETCH_SCHEMA_SQL = """
SELECT
    s.table_name,
    s.table_schema,
    s.table_catalog,
    s.table_type,
    s.table_comment,
    s.table_character_set_name,
    s.table_collation_name,
    s.table_row_count,
    s.table_size,
    s.table_created_at,
    s.table_updated_at,
    s.table_engine,
    s.table_version,
    s.table_row_format,
    s.table_auto_increment,
    s.table_comment,
    s.table_character_set_name,
    s.table_collation_name,
    s.*
FROM
    information_schema.schemata s
WHERE
    s.schema_name NOT LIKE 'pg_%'
    AND s.schema_name NOT LIKE 'pg_internal'
    AND s.schema_name NOT LIKE 'information_schema'
    AND s.schema_name NOT LIKE 'pg_test'
    AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) !~ '{normalized_exclude_regex}'
    
    AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) ~ '{normalized_include_regex}';
"""

FETCH_TABLE_SQL = """
SELECT
    t.*
FROM
    information_schema.tables t
WHERE concat(current_database(), concat('.', t.table_schema)) !~ '{normalized_exclude_regex}'
    AND concat(current_database(), concat('.', t.table_schema)) ~ '{normalized_include_regex}'
    {temp_table_regex_sql};
"""

FETCH_COLUMN_SQL = """
SELECT
    c.*
FROM
    information_schema.columns c
WHERE
    concat(current_database(), concat('.', c.table_schema)) !~ '{normalized_exclude_regex}'
    AND concat(current_database(), concat('.', c.table_schema)) ~ '{normalized_include_regex}'
    {temp_table_regex_sql};
"""

# Table Filtering Regex SQL
TABLES_EXTRACTION_TEMP_TABLE_REGEX_SQL = "AND t.table_name !~ '{exclude_table_regex}'"
COLUMN_EXTRACTION_TEMP_TABLE_REGEX_SQL = "AND c.table_name !~ '{exclude_table_regex}'"
TEMP_TABLE_REGEX_SQL = "AND t.table_name !~ '{exclude_table_regex}'"

# Validation Queries
TABLES_CHECK_SQL = """
SELECT count(*)
    FROM INFORMATION_SCHEMA.TABLES
    WHERE concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) !~ '{normalized_exclude_regex}'
        AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) ~ '{normalized_include_regex}'
        AND TABLE_SCHEMA NOT IN ('performance_schema', 'information_schema', 'pg_catalog', 'pg_internal')
        {temp_table_regex_sql};
"""

# General Metadata Queries
METADATA_SQL = """
SELECT schema_name, catalog_name
    FROM INFORMATION_SCHEMA.SCHEMATA
    WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'
""" 