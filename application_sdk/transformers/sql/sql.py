
class Database:
    query = """
        SELECT 
            dataframe.database_name AS name,
            CONCAT(connection_qualified_name, '/', dataframe.database_name) AS qualified_name,
            dataframe.connection_qualified_name AS connection_qualified_name
        FROM 
            dataframe
    """

class Table:
    query = """
        SELECT 
            table_name AS name,
            concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', table_name) AS qualified_name,
            connection_qualified_name,
            table_catalog AS database_name,
            concat(connection_qualified_name, '/', table_catalog) AS database_qualified_name,
            table_schema AS schema_name,
            concat(connection_qualified_name, '/', table_catalog, '/', table_schema) AS schema_qualified_name,
            view_definition AS definition,
            column_count,
            row_count,
            -- COALESCE(size_bytes, 0) AS size_bytes,
            table_type,
            CASE 
                WHEN is_partition = 'YES' THEN 'TablePartition'
                WHEN table_type IN ('TABLE', 'BASE TABLE', 'FOREIGN TABLE', 'PARTITIONED TABLE') THEN 'Table'
                WHEN table_type = 'MATERIALIZED VIEW' THEN 'MaterialisedView'
                -- WHEN table_type = 'DYNAMIC TABLE' OR is_dynamic = 'YES' THEN 'SnowflakeDynamicTable'
                ELSE 'View'
            END AS entity_class,
            CASE 
                WHEN is_partition = 'YES' THEN parent_table_name
                ELSE NULL
            END AS table_name,
            CASE 
                WHEN is_partition = 'YES' THEN concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', parent_table_name)
                ELSE NULL
            END AS table_qualified_name
            -- location AS external_location,
            -- file_format_type AS external_location_format,
            -- stage_region AS external_location_region,
            -- refresh_mode,
            -- staleness,
            -- stale_since_date,
            -- refresh_method,
            -- is_transient,
            -- table_catalog_id,
            -- table_schema_id,
            -- last_ddl,
            -- last_ddl_by,
            -- is_secure,
            -- retention_time,
            -- stage_url,
            -- is_insertable_into,
            -- number_columns_in_part_key,
            -- columns_participating_in_part_key,
            -- is_typed,
            -- auto_clustering_on,
            -- engine,
            -- auto_increment
        FROM 
            dataframe
    """
