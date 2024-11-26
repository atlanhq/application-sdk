import logging
import re
from typing import Any, Callable, Dict, Optional, Union

from pyatlan.model.assets import (
    SQL,
    Column,
    Database,
    Function,
    MaterialisedView,
    Schema,
    SnowflakeDynamicTable,
    SnowflakePipe,
    SnowflakeStream,
    SnowflakeTag,
    Table,
    TagAttachment,
    View,
)

from application_sdk.workflows.transformers import TransformerInterface

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AtlasTransformer(TransformerInterface):
    """
    AtlasTransformer is a class that transforms metadata into Atlas entities.
    It uses the pyatlan library to create the entities.

    Attributes:
        timestamp (str): The timestamp of the metadata.

    Usage:
        Subclass this class and override the transform_metadata method to customize the transformation process.
        Then use the subclass as an argument to the SQLWorkflowWorker.

        >>> class CustomAtlasTransformer(AtlasTransformer):
        >>>     def transform_metadata(self, typename: str, data: Dict[str, Any], **kwargs: Any) -> Optional[str]:
        >>>         # Custom logic here
    """

    def __init__(self, connector_name: str, **kwargs: Any):
        self.current_epoch = kwargs.get("current_epoch", "0")
        self.connector_name = connector_name

    def transform_metadata(
        self, typename: str, data: Dict[str, Any], **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        base_qualified_name = kwargs.get(
            "base_qualified_name", f"default/{self.connector_name}/{self.current_epoch}"
        )

        entity_creators: Dict[str, Callable[[Dict[str, Any], str], Optional[SQL]]] = {
            "DATABASE": self._create_database_entity,
            "SCHEMA": self._create_schema_entity,
            "TABLE": self._create_table_entity,
            "COLUMN": self._create_column_entity,
            "PIPE": self._create_pipe_entity,
            "FUNCTION": self._create_function_entity,
            "TAG": self._create_tag_entity,
            "TAG_REF": self._create_tag_ref_entity,
            "STREAM": self._create_stream_entity,
        }

        creator = entity_creators.get(typename.upper())
        if creator:
            entity = creator(data, base_qualified_name)
            return entity.dict()
        else:
            logger.error(f"Unknown typename: {typename}")
            return None

    def _create_database_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[Database]:
        try:
            assert data["datname"] is not None, "Database name cannot be None"

            sql_database = Database.creator(
                name=data["datname"],
                connection_qualified_name=f"{base_qualified_name}",
            )

            sql_database.attributes.schema_count = data.get("schema_count", 0)

            # TODO: These are not available in the attributes or database entity
            # sql_database.attributes.description = data.get("remarks", "")
            # sql_database.attributes.last_sync_run_at = data.get("lastSyncRunAt", None)
            # sql_database.attributes.last_sync_workflow_name = data.get("lastSyncWorkflowName", None)
            # sql_database.attributes.last_sync_run = data.get("lastSyncRun", None)
            # sql_database.attributes.source_created_by = data.get("source_created_by", "")
            # sql_database.attributes.source_created_at = data.get("source_created_at", "")
            # sql_database.attributes.source_updated_at = data.get("source_updated_at", "")
            # sql_database.attributes.source_id = data.get("source_id", "")
            # sql_database.attributes.tenant_id = data.get("tenant_id", "")

            if data.get("created", None):
                sql_database.attributes.source_created_at = data.get("created")

            if data.get("last_altered", None):
                sql_database.attributes.source_updated_at = data.get("last_altered")

            return sql_database
        except AssertionError as e:
            logger.error(f"Error creating DatabaseEntity: {str(e)}")
            return None

    def _create_schema_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[Schema]:
        try:
            assert data["schema_name"] is not None, "Schema name cannot be None"
            assert data["catalog_name"] is not None, "Catalog name cannot be None"
            sql_schema = Schema.creator(
                name=data["schema_name"],
                database_qualified_name=f"{base_qualified_name}/{data['catalog_name']}",
            )

            if table_count := data.get("table_count", None):
                sql_schema.table_count = table_count

            if views_count := data.get("view_count", None):
                sql_schema.views_count = views_count

            if remarks := data.get("remarks", None):
                sql_schema.description = remarks

            if created := data.get("created", None):
                sql_schema.source_created_at = created

            if last_altered := data.get("last_altered", None):
                sql_schema.source_updated_at = last_altered

            if schema_owner := data.get("schema_owner", None):
                sql_schema.source_created_by = schema_owner

            if schema_id := data.get("schema_id", None):
                sql_schema.source_id = schema_id

            if catalog_id := data.get("catalog_id", None):
                sql_schema.catalog_id = catalog_id

            if is_managed_access := data.get("is_managed_access", None):
                sql_schema.is_managed_access = is_managed_access
            sql_schema.attributes.database = Database.creator(
                name=data["catalog_name"],
                connection_qualified_name=f"{base_qualified_name}",
            )
            return sql_schema
        except AssertionError as e:
            logger.error(f"Error creating SchemaEntity: {str(e)}")
            return None

    def _create_table_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[Union[Table, View, MaterialisedView]]:
        try:
            entity = None
            if data.get("table_type") == "MATERIALIZED VIEW":
                entity = MaterialisedView.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                    connection_qualified_name=base_qualified_name,
                    database_qualified_name=f"{base_qualified_name}/{data['table_catalog']}",
                    schema_name=data["table_schema"],
                    database_name=data["table_catalog"],
                )
            elif data.get("table_type") == "VIEW":
                entity = View.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                    connection_qualified_name=base_qualified_name,
                    database_qualified_name=f"{base_qualified_name}/{data['table_catalog']}",
                    schema_name=data["table_schema"],
                    database_name=data["table_catalog"],
                )
            elif (
                data.get("table_type") == "DYNAMIC TABLE"
                or data.get("is_dynamic") == "YES"
            ):
                entity = SnowflakeDynamicTable.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                    connection_qualified_name=base_qualified_name,
                    database_qualified_name=f"{base_qualified_name}/{data['table_catalog']}",
                    schema_name=data["table_schema"],
                    database_name=data["table_catalog"],
                )
            else:
                entity = Table.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                    connection_qualified_name=base_qualified_name,
                    database_qualified_name=f"{base_qualified_name}/{data['table_catalog']}",
                    schema_name=data["table_schema"],
                    database_name=data["table_catalog"],
                )

            if data.get("remarks", None) and isinstance(data["remarks"], str):
                entity.description = data["remarks"]

            entity.last_sync_run_at = data.get("now")

            # TODO: Don't have workflow_name, crawler_name, tenant_id in the metadata
            # entity.last_sync_workflow_name = data.get("crawler_name")
            # entity.last_sync_run = data.get("workflow_name")
            # entity.tenant_id = data.get("tenant_id")

            if column_count := data.get("column_count", None):
                entity.column_count = column_count

            if source_id := data.get("TABLE_ID", None):
                entity.source_id = source_id

            if catalog_id := data.get("TABLE_CATALOG_ID", None):
                entity.catalog_id = catalog_id

            if schema_id := data.get("TABLE_SCHEMA_ID", None):
                entity.schema_id = schema_id

            if last_ddl := data.get("LAST_DDL", None):
                entity.last_ddl = last_ddl

            if last_ddl_by := data.get("LAST_DDL_BY", None):
                entity.last_ddl_by = last_ddl_by

            if is_secure := data.get("IS_SECURE", None):
                entity.is_secure = is_secure

            if retention_time := data.get("RETENTION_TIME", None):
                entity.retention_time = retention_time

            if stage_url := data.get("STAGE_URL", None):
                entity.stage_url = stage_url

            if is_insertable_into := data.get("IS_INSERTABLE_INTO", None):
                entity.is_insertable_into = is_insertable_into

            if num_part_key_cols := data.get("NUMBER_COLUMNS_IN_PART_KEY", None):
                entity.number_columns_in_part_key = num_part_key_cols

            if part_key_cols := data.get("COLUMNS_PARTICIPATING_IN_PART_KEY", None):
                entity.columns_participating_in_part_key = part_key_cols

            if is_typed := data.get("IS_TYPED", None):
                entity.is_typed = is_typed

            if auto_clustering := data.get("AUTO_CLUSTERING_ON", None):
                entity.auto_clustering_on = auto_clustering

            if engine := data.get("ENGINE", None):
                entity.engine = engine

            if auto_increment := data.get("AUTO_INCREMENT", None):
                entity.auto_increment = auto_increment

            if row_count := data.get("row_count", None):
                entity.row_count = row_count

            if bytes_size := data.get("bytes", None):
                entity.size_bytes = bytes_size

            if is_transient := data.get("is_transient", None):
                entity.custom_metadata = {"is_transient": is_transient}

            entity.attributes.atlan_schema = Schema.creator(
                name=data["table_schema"],
                database_qualified_name=f"{base_qualified_name}/{data['table_catalog']}",
            )

            if view_definition := data.get("VIEW_DEFINITION", None):
                if isinstance(view_definition, list) and view_definition:
                    view_def_values = list(view_definition[0].values())
                    if view_def_values:
                        entity.definition = view_def_values[0]
                    else:
                        entity.definition = ""
                else:
                    entity.definition = str(view_definition)

            if table_owner := data.get("TABLE_OWNER", None):
                entity.source_created_by = table_owner

            if created_at := data.get("CREATED", None):
                entity.source_created_at = created_at

            if last_altered := data.get("LAST_ALTERED", None):
                entity.source_updated_at = last_altered

            if table_id := data.get("TABLE_ID", None):
                entity.source_id = table_id
                entity.catalog_id = data.get("TABLE_CATALOG_ID")
                entity.schema_id = data.get("TABLE_SCHEMA_ID")

            if last_ddl := data.get("LAST_DDL", None):
                entity.last_ddl = last_ddl

            if last_ddl_by := data.get("LAST_DDL_BY", None):
                entity.last_ddl_by = last_ddl_by

            if is_secure := data.get("IS_SECURE", None):
                entity.is_secure = is_secure

            if retention_time := data.get("RETENTION_TIME", None):
                entity.retention_time = retention_time

            if stage_url := data.get("STAGE_URL", None):
                entity.stage_url = stage_url

            if is_insertable := data.get("IS_INSERTABLE_INTO", None):
                entity.is_insertable_into = is_insertable

            if num_part_cols := data.get("NUMBER_COLUMNS_IN_PART_KEY", None):
                entity.number_columns_in_part_key = num_part_cols

            return entity
        except AssertionError as e:
            logger.error(f"Error creating TableEntity: {str(e)}")
            return None

    def _create_column_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[Column]:
        try:
            # TODO: For all types, which are required attributes, and which aren't
            assert data["column_name"] is not None, "Column name cannot be None"
            assert data["table_catalog"] is not None, "Table catalog cannot be None"
            assert data["table_schema"] is not None, "Table schema cannot be None"
            assert data["table_name"] is not None, "Table name cannot be None"
            assert (
                data["ordinal_position"] is not None
            ), "Ordinal position cannot be None"
            assert data["data_type"] is not None, "Data type cannot be None"

            view_definition = data.get("view_definition", "")
            if isinstance(view_definition, list) and view_definition:
                view_definition_values = view_definition[0].values()
                view_definition = (
                    list(view_definition_values)[0] if view_definition_values else ""
                )

            is_materialized = False
            if view_definition:
                materialized_pattern = (
                    r"create( )+(or replace( )+)?(secure( )+)?materialized view"
                )
                is_materialized = bool(
                    re.search(materialized_pattern, view_definition.lower())
                )

            parent_type = None
            if data.get("table_type") == "MATERIALIZED VIEW" or is_materialized:
                parent_type = MaterialisedView
            elif data.get("table_type") == "VIEW":
                parent_type = View
            elif (
                data.get("table_type") in ("DYNAMIC TABLE", "DYNAMIC_TABLE")
                or data.get("is_dynamic") == "YES"
            ):
                parent_type = SnowflakeDynamicTable
            else:
                parent_type = Table
            sql_column = Column.creator(
                name=data["column_name"],
                parent_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}",
                parent_type=parent_type,
                order=data["ordinal_position"],
            )

            # TODO: The description is not available in the attributes or column entity
            # remarks = data.get('REMARKS')
            # comment = data.get('COMMENT')
            # if not (remarks and isinstance(remarks, str)) and comment:
            #     # TODO: striptags from jinja
            #     sql_column.attributes.description = comment[:100000]

            sql_column.is_nullable = data.get("is_nullable", "YES") == "YES"
            sql_column.is_partition = data.get("is_partition") == "YES"
            if sql_column.is_partition:
                sql_column.partition_order = data.get("partition_order", 0)

            sql_column.is_primary = data.get("primary_key") == "YES"
            sql_column.is_foreign = data.get("foreign_key") == "YES"

            if data.get("character_maximum_length", None) is not None:
                sql_column.max_length = data.get("character_maximum_length", 0)
            if data.get("numeric_precision", None) is not None:
                sql_column.precision = data.get("numeric_precision", 0)
            if data.get("numeric_scale", None) is not None:
                sql_column.numeric_scale = data.get("numeric_scale", 0)

            if data.get("table_type") == "MATERIALIZED VIEW" or is_materialized:
                sql_column.attributes.view_name = data["table_name"]
                sql_column.attributes.view_qualified_name = f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}"
                sql_column.attributes.materialised_view = MaterialisedView.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )
            elif data.get("table_type") == "VIEW":
                sql_column.attributes.view_name = data["table_name"]
                sql_column.attributes.view_qualified_name = f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}"
                sql_column.attributes.view = View.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )
            elif (
                data.get("table_type") in ("DYNAMIC TABLE", "DYNAMIC_TABLE")
                or data.get("is_dynamic") == "YES"
            ):
                sql_column.attributes.table_name = data["table_name"]
                sql_column.attributes.table_qualified_name = f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}"
                sql_column.attributes.snowflake_dynamic_table = SnowflakeDynamicTable.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )
            else:
                sql_column.attributes.table_name = data["table_name"]
                sql_column.attributes.table_qualified_name = f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}"
                sql_column.attributes.table = Table.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )

            if data.get("column_id"):
                sql_column.source_id = data.get("column_id")
                sql_column.catalog_id = data.get("table_catalog_id")
                sql_column.schema_id = data.get("table_schema_id")
                sql_column.table_id = data.get("table_id")

            if data.get("character_octet_length") is not None:
                sql_column.character_octet_length = data.get("character_octet_length")

            sql_column.is_auto_increment = data.get("is_autoincrement") == "YES"
            sql_column.is_generated = data.get("is_generatedcolumn") == "YES"

            if data.get("extra_info"):
                sql_column.extra_info = data.get("extra_info")
            if data.get("buffer_length") is not None:
                sql_column.buffer_length = data.get("buffer_length")
            if data.get("column_size") is not None:
                sql_column.column_size = data.get("column_size")

            return sql_column
        except AssertionError as e:
            logger.error(f"Error creating ColumnEntity: {str(e)}")
            return None

    def _create_pipe_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[SnowflakePipe]:
        try:
            assert data["pipe_name"] is not None, "Pipe name cannot be None"
            assert data["definition"] is not None, "Pipe definition cannot be None"

            snowflake_pipe = SnowflakePipe.create(
                name=data["pipe_name"],
                definition=data["definition"],
                snowflake_pipe_is_auto_ingest_enabled=data.get(
                    "is_autoingest_enabled", None
                ),
                snowflake_pipe_notification_channel_name=data.get(
                    "notification_channel_name", None
                ),
            )

            snowflake_pipe.attributes.atlan_schema = Schema.creator(
                name=data["pipe_schema"],
                database_qualified_name=f"{base_qualified_name}/{data['pipe_catalog']}",
            )

            return snowflake_pipe
        except AssertionError as e:
            logger.error(f"Error creating ColumnEntity: {str(e)}")
            return None

    def _create_function_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[Function]:
        try:
            assert data["function_name"] is not None, "Function name cannot be None"
            assert (
                data["argument_signature"] is not None
            ), "Function argument signature cannot be None"
            assert (
                data["function_definition"] is not None
            ), "Function definition cannot be None"
            assert (
                data["is_external"] is not None
            ), "Function is_external name cannot be None"
            assert (
                data["is_memoizable"] is not None
            ), "Function is_memoizable cannot be None"
            assert (
                data["function_language"] is not None
            ), "Function language cannot be None"

            data_type = data.get("data_type", "")
            function_type = "Scalar"
            if "table" in data_type:
                function_type = "TABLE"

            # TODO: Creator has not been implemented yet
            function = Function.create(
                name=data["function_name"],
                function_arguments=data["argument_signature"][1:-1].split(", "),
                function_definition=data["function_definition"],
                function_language=data["function_language"],
                function_return_type=data_type,
                function_type=function_type,
            )

            if data.get("is_secure") is not None:
                function.attributes.function_is_secure = data.get("is_secure") == "YES"

            if data.get("is_external", None) is not None:
                function.attributes.function_is_external = (
                    data.get("is_external") == "YES"
                )

            if data.get("is_data_metric", None) is not None:
                function.attributes.function_is_d_m_f = (
                    data.get("is_data_metric") == "YES"
                )

            if data.get("is_memoizable", None) is not None:
                function.attributes.function_is_memoizable = (
                    data.get("is_memoizable") == "YES"
                )

            return function
        except AssertionError as e:
            logger.error(f"Error creating ColumnEntity: {str(e)}")
            return None

    def _create_tag_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[SnowflakeTag]:
        try:
            assert data["tag_name"] is not None, "Tag name cannot be None"
            assert data["tag_id"] is not None, "Tag id cannot be None"

            # TODO: Creator has not been implemented yet
            tag = SnowflakeTag.create(
                name=data["tag_name"],
                tag_id=data["tag_id"],
                allowed_values=data.get("tag_allowed_values", []),
                source_updated_at=data["last_altered"],
            )

            tag.attributes.atlan_schema = Schema.creator(
                name=data["tag_schema"],
                database_qualified_name=f"{base_qualified_name}/{data['tag_database']}",
            )

            return tag
        except AssertionError as e:
            logger.error(f"Error creating ColumnEntity: {str(e)}")
            return None

    def _create_tag_ref_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[TagAttachment]:
        try:
            assert data["tag_name"] is not None, "Tag name cannot be None"

            # TODO: Creator has not been implemented yet
            tag_attachment = TagAttachment.create(
                name=data["tag_name"],
                tag_attachment_string_value=data["tag_value"],
            )

            return tag_attachment
        except AssertionError as e:
            logger.error(f"Error creating ColumnEntity: {str(e)}")
            return None

    def _create_stream_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[SnowflakeStream]:
        try:
            assert data["name"] is not None, "Stream name cannot be None"
            assert data["type"] is not None, "Stream type cannot be None"
            assert data["source_type"] is not None, "Stream source type cannot be None"
            assert data["mode"] is not None, "Stream mode cannot be None"
            assert data["stale"] is not None, "Stream stale cannot be None"
            assert data["stale_after"] is not None, "Stream stale after cannot be None"

            # TODO: description
            # TODO: Creator has not been implemented yet
            snowflake_stream = SnowflakeStream.create(
                name=data["name"],
                stream_type=data["type"],
                stream_source_type=data["source_type"],
                stream_mode=data["mode"],
                stream_is_stale=data["stale"],
                stream_stale_after=data["stale_after"],
            )

            snowflake_stream.attributes.atlan_schema = Schema.creator(
                name=data["schema_name"],
                database_qualified_name=f"{base_qualified_name}/{data['database_name']}",
            )

            return snowflake_stream
        except AssertionError as e:
            logger.error(f"Error creating ColumnEntity: {str(e)}")
            return None
