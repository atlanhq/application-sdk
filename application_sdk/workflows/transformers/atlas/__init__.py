import logging
from typing import Any, Callable, Dict, Optional, Union

from pyatlan.model.assets import (
    SQL,
    Column,
    Database,
    Function,
    Schema,
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
            sql_schema.attributes.table_count = data.get("table_count", 0)
            sql_schema.attributes.views_count = data.get("view_count", 0)
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
    ) -> Optional[Union[Table, View]]:
        try:
            assert data["table_name"] is not None, "Table name cannot be None"
            assert data["table_catalog"] is not None, "Table catalog cannot be None"
            assert data["table_schema"] is not None, "Table schema cannot be None"

            sql_table = None

            if data.get("table_type") == "TABLE":
                sql_table: Table = Table.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )

                if data.get("is_iceberg") == "YES":
                    sql_table.attributes.table_type = "ICEBERG"
                elif data.get("is_temporary") == "YES":
                    sql_table.attributes.table_type = "TEMPORARY"

                if iceberg_table_type := data.get("iceberg_table_type", "").upper():
                    sql_table.attributes.iceberg_table_type = iceberg_table_type
                if catalog_name := data.get("catalog_name"):
                    sql_table.attributes.iceberg_catalog_name = catalog_name
                if catalog_table_name := data.get("catalog_table_name"):
                    sql_table.attributes.iceberg_catalog_table_name = catalog_table_name
                if catalog_namespace := data.get("catalog_namespace"):
                    sql_table.attributes.iceberg_catalog_table_namespace = (
                        catalog_namespace
                    )
                if external_volume := data.get("external_volume_name"):
                    sql_table.attributes.table_external_volume_name = external_volume
                if base_location := data.get("base_location"):
                    sql_table.attributes.iceberg_table_base_location = base_location
                if catalog_source := data.get("catalog_source"):
                    sql_table.attributes.iceberg_catalog_source = catalog_source
                if retention_time := data.get("retention_time", -1):
                    if retention_time != -1:
                        sql_table.attributes.table_retention_time = retention_time

                if data.get("is_dynamic") == "YES":
                    view_definition = data.get("view_definition_list", "")
                    if isinstance(view_definition, list) and view_definition:
                        values = list(view_definition[0].values())
                        sql_table.attributes.definition = values[0] if values else ""
                    else:
                        sql_table.attributes.definition = str(view_definition)
                else:
                    sql_table.attributes.definition = data.get("view_definition", "")

            else:
                sql_table: View = View.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )
                sql_table.attributes.definition = data.get("VIEW_DEFINITION", "")
            sql_table.attributes.atlan_schema = Schema.creator(
                name=data["table_schema"],
                database_qualified_name=f"{base_qualified_name}/{data['table_catalog']}",
            )
            sql_table.attributes.column_count = data.get("column_count", 0)
            sql_table.attributes.row_count = data.get("row_count", 0)
            sql_table.attributes.size_bytes = data.get("size_bytes", 0)
            return sql_table
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

            parent_type = None
            if data.get("table_type") == "TABLE":
                parent_type = Table
            else:
                parent_type = View

            sql_column = Column.creator(
                name=data["column_name"],
                parent_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}",
                parent_type=parent_type,
                order=data["ordinal_position"],
                # TODO: In the column extraction, we don't have is_partition, primary_key, is_foreign
                # is_partition=data["is_partition"] == "YES",
                # is_primary=data["primary_key"] == "YES",
                # is_foreign=data["is_foreign"] == "YES",
                max_length=data.get("character_maximum_length", 0),
                precision=data.get("numeric_precision", 0),
                numeric_scale=data.get("numeric_scale", 0),
            )
            sql_column.attributes.data_type = data.get("data_type", "")
            sql_column.attributes.is_nullable = data.get("is_nullable", "YES") == "YES"
            if data.get("table_type") == "TABLE":
                sql_column.attributes.table = Table.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )
            else:
                sql_column.attributes.view = View.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )
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

            # TODO: Do we need to fill last_sync_run_at, last_sync_run, source_updated_at, etc.
            # TODO: Creator has not been implemented yet
            tag = SnowflakeTag.create(
                name=data["tag_name"],
                tag_id=data["tag_id"],
                allowed_values=data.get("tag_allowed_values", []),
                source_updated_at=data["last_altered"],
            )

            # TODO: Is this required?
            tag.attributes.atlan_schema = Schema.creator(
                name=data["tag_schema"],
                database_qualified_name=f"{base_qualified_name}/{data['tag_catalog']}",
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
