import json
import logging
import re
from datetime import datetime
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


# TODO: Move this somewhere else
def process_text(text: str, max_length: int = 100000) -> str:
    if len(text) > max_length:
        text = text[:max_length] + "..."

    text = re.sub(r"<[^>]+>", "", text)

    text = json.dumps(text)

    return text


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
            # TODO:
            # "lastSyncWorkflowName": "{{external_map['crawler_name']}}",
            # "lastSyncRun": "{{external_map['workflow_name']}}",
            # "tenantId": "{{external_map['tenant_id']}}"

            assert data["datname"] is not None, "Database name cannot be None"

            sql_database = Database.creator(
                name=json.dumps(data["datname"]),
                connection_qualified_name=f"{base_qualified_name}",
            )

            if schema_count := data.get("schema_count", None):
                sql_database.schema_count = schema_count

            if remarks := data.get("remarks", None) or data.get("comment", None):
                sql_database.description = process_text(remarks)

            if last_sync_workflow_name := data.get("lastSyncWorkflowName", None):
                sql_database.last_sync_workflow_name = last_sync_workflow_name

            sql_database.last_sync_run_at = datetime.now()

            if source_created_by := data.get("database_owner", None):
                sql_database.source_created_by = source_created_by

            if created := data.get("created", None):
                sql_database.source_created_at = datetime.strptime(
                    created, "%Y-%m-%dT%H:%M:%S.%f%z"
                )

            if last_altered := data.get("last_altered", None):
                sql_database.source_updated_at = datetime.strptime(
                    last_altered, "%Y-%m-%dT%H:%M:%S.%f%z"
                )

            if not sql_database.custom_attributes:
                sql_database.custom_attributes = {}

            if database_id := data.get("database_id", None):
                sql_database.custom_attributes["source_id"] = database_id

            if extra_info := data.get("extra_info", []):
                if len(extra_info) > 0:
                    if comment := extra_info[0].get("comment"):
                        sql_database.description = process_text(comment)[:100000]

                    if database_owner := extra_info[0].get("database_owner"):
                        sql_database.source_created_by = database_owner

                    if created := extra_info[0].get("created"):
                        sql_database.source_created_at = datetime.strptime(
                            created, "%Y-%m-%dT%H:%M:%S.%f%z"
                        )

                    if last_altered := extra_info[0].get("last_altered"):
                        sql_database.source_updated_at = datetime.strptime(
                            last_altered, "%Y-%m-%dT%H:%M:%S.%f%z"
                        )

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

            # TODO:
            # "lastSyncWorkflowName": "{{external_map['crawler_name']}}",
            # "lastSyncRun": "{{external_map['workflow_name']}}",
            # "tenantId": "{{external_map['tenant_id']}}",

            sql_schema = Schema.creator(
                name=json.dumps(data["schema_name"]),
                database_qualified_name=f"{base_qualified_name}/{data['catalog_name']}",
            )
            sql_schema.database_name = data["catalog_name"]

            if table_count := data.get("table_count", None):
                sql_schema.table_count = table_count

            if views_count := data.get("view_count", None):
                sql_schema.views_count = views_count

            if remarks := data.get("remarks", None) or data.get("comment", None):
                sql_schema.description = process_text(remarks)

            if created := data.get("created", None):
                sql_schema.source_created_at = datetime.strptime(
                    created, "%Y-%m-%dT%H:%M:%S.%f%z"
                )

            if last_altered := data.get("last_altered", None):
                sql_schema.source_updated_at = datetime.strptime(
                    last_altered, "%Y-%m-%dT%H:%M:%S.%f%z"
                )

            if schema_owner := data.get("schema_owner", None):
                sql_schema.source_created_by = schema_owner

            if not sql_schema.custom_attributes:
                sql_schema.custom_attributes = {}

            if schema_id := data.get("schema_id", None):
                sql_schema.custom_attributes["source_id"] = schema_id

            if catalog_id := data.get("catalog_id", None):
                sql_schema.custom_attributes["catalog_id"] = catalog_id

            if is_managed_access := data.get("is_managed_access", None):
                sql_schema.custom_attributes["is_managed_access"] = is_managed_access

            sql_schema.attributes.database = Database.creator(
                name=json.dumps(data["catalog_name"]),
                connection_qualified_name=f"{base_qualified_name}",
            )

            sql_schema.last_sync_run_at = datetime.now()
            return sql_schema
        except AssertionError as e:
            logger.error(f"Error creating SchemaEntity: {str(e)}")
            return None

    def _create_table_entity(
        self, data: Dict[str, Any], base_qualified_name: str
    ) -> Optional[Union[Table, View, MaterialisedView]]:
        try:
            assert data["table_name"] is not None, "Table name cannot be None"
            assert data["table_cat"] is not None, "Table catalog cannot be None"
            assert data["table_schem"] is not None, "Table schema cannot be None"
            assert data["table_type"] is not None, "Table type cannot be None"
            assert data["table_owner"] is not None, "Table owner cannot be None"

            # TODO:
            # entity.last_sync_run = last_sync_run
            # entity.last_sync_workflow_name = data.get("crawler_name")
            # entity.tenant_id = data.get("tenant_id")
            entity = None
            if data.get("table_type") == "MATERIALIZED VIEW":
                entity = MaterialisedView.creator(
                    name=json.dumps(data["table_name"]),
                    schema_qualified_name=f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}",
                    connection_qualified_name=base_qualified_name,
                    database_qualified_name=f"{base_qualified_name}/{data['table_cat']}",
                    schema_name=data["table_schem"],
                    database_name=data["table_catalog"],
                )
            elif data.get("table_type") == "VIEW":
                entity = View.creator(
                    name=json.dumps(data["table_name"]),
                    schema_qualified_name=f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}",
                    connection_qualified_name=base_qualified_name,
                    database_qualified_name=f"{base_qualified_name}/{data['table_cat']}",
                    schema_name=data["table_schem"],
                    database_name=data["table_cat"],
                )
            elif (
                data.get("table_type") == "DYNAMIC TABLE"
                or data.get("is_dynamic") == "YES"
            ):
                entity = SnowflakeDynamicTable.creator(
                    name=json.dumps(data["table_name"]),
                    schema_qualified_name=f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}",
                    connection_qualified_name=base_qualified_name,
                    database_qualified_name=f"{base_qualified_name}/{data['table_cat']}",
                    schema_name=data["table_schem"],
                    database_name=data["table_cat"],
                )
            else:
                entity = Table.creator(
                    name=json.dumps(data["table_name"]),
                    schema_qualified_name=f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}",
                    connection_qualified_name=base_qualified_name,
                    database_qualified_name=f"{base_qualified_name}/{data['table_cat']}",
                    schema_name=data["table_schem"],
                    database_name=data["table_cat"],
                )

            if remarks := data.get("remarks", None) or data.get("comment", None):
                entity.description = process_text(remarks)

            entity.last_sync_run_at = datetime.now()

            if column_count := data.get("column_count", None):
                entity.column_count = round(int(column_count))

            if row_count := data.get("row_count", None):
                entity.row_count = round(int(row_count))

            if bytes_size := data.get("bytes", None):
                entity.size_bytes = bytes_size

            entity.attributes.atlan_schema = Schema.creator(
                name=json.dumps(data["table_schem"]),
                database_qualified_name=f"{base_qualified_name}/{data['table_cat']}",
            )

            if view_definition := data.get("view_definition", ""):
                if view_definition and isinstance(view_definition, list):
                    view_def_values = list(view_definition[0].values())
                    if view_def_values:
                        entity.definition = json.dumps(view_def_values[0])
                    else:
                        entity.definition = ""
                else:
                    entity.definition = json.dumps(str(view_definition))

            entity.source_created_by = data["table_owner"]

            if created_at := data.get("created", None):
                entity.source_created_at = datetime.strptime(
                    created_at, "%Y-%m-%dT%H:%M:%S.%f%z"
                )

            if last_altered := data.get("last_altered", None):
                entity.source_updated_at = datetime.strptime(
                    last_altered, "%Y-%m-%dT%H:%M:%S.%f%z"
                )

            # Custom attributes
            if not entity.custom_attributes:
                entity.custom_attributes = {}

            if is_transient := data.get("is_transient", None):
                entity.custom_attributes["is_transient"] = is_transient

            if table_id := data.get("table_id", None):
                entity.custom_attributes["source_id"] = table_id
                entity.custom_attributes["catalog_id"] = data.get("table_catalog_id")
                entity.custom_attributes["schema_id"] = data.get("table_schema_id")
                pass

            if last_ddl := data.get("last_ddl", None):
                entity.custom_attributes["last_ddl"] = last_ddl
            if last_ddl_by := data.get("last_ddl_by", None):
                entity.custom_attributes["last_ddl_by"] = last_ddl_by

            entity.custom_attributes["is_secure"] = data.get("is_secure", None)
            entity.custom_attributes["retention_time"] = data.get(
                "retention_time", None
            )
            entity.custom_attributes["stage_url"] = data.get("stage_url", None)
            entity.custom_attributes["is_insertable_into"] = data.get(
                "is_insertable_into", None
            )
            entity.custom_attributes["number_columns_in_part_key"] = data.get(
                "number_columns_in_part_key", None
            )
            entity.custom_attributes["columns_participating_in_part_key"] = data.get(
                "columns_participating_in_part_key", None
            )
            entity.custom_attributes["is_typed"] = data.get("is_typed", None)
            entity.custom_attributes["auto_clustering_on"] = data.get(
                "auto_clustering_on", None
            )
            entity.custom_attributes["engine"] = data.get("engine", None)
            entity.custom_attributes["auto_increment"] = data.get(
                "auto_increment", None
            )

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
            assert data["table_type"] is not None, "Table type cannot be None"
            assert data["table_cat"] is not None, "Table catalog cannot be None"
            assert data["table_schem"] is not None, "Table schema cannot be None"
            assert data["table_name"] is not None, "Table name cannot be None"
            assert data["data_type"] is not None, "Data type cannot be None"

            # TODO:
            # "lastSyncWorkflowName": "{{external_map['crawler_name']}}",
            # "lastSyncRun": "{{external_map['workflow_name']}}",
            # "tenantId": "{{external_map['tenant_id']}}",

            view_definition = data.get("view_definition", "")
            if isinstance(view_definition, list) and view_definition:
                view_definition_values = view_definition[0].values()
                view_definition = (
                    json.dumps(list(view_definition_values)[0])
                    if view_definition_values
                    else ""
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

            order = None
            if (
                data.get("ordinal_position", None)
                or data.get("column_id", None)
                or data.get("internal_column_id", None)
            ):
                order = data.get(
                    "ordinal_position",
                    data.get("column_id", data.get("internal_column_id", "")),
                )

            sql_column = Column.creator(
                name=json.dumps(data["column_name"]),
                parent_qualified_name=f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}/{data['table_name']}",
                parent_type=parent_type,
                order=int(order),
            )
            sql_column.database_name = data["table_cat"]
            sql_column.schema_name = data["table_schem"]

            if remarks := data.get("remarks", None) or data.get("comment", None):
                sql_column.description = process_text(remarks)

            if nullable := data.get("is_nullable", None):
                sql_column.is_nullable = nullable == "YES"

            if is_partition := data.get("is_partition", None):
                sql_column.is_partition = is_partition == "YES"
                if sql_column.is_partition:
                    sql_column.partition_order = data.get("partition_order", 0)

            if primary_key := data.get("primary_key", None):
                sql_column.is_primary = primary_key == "YES"

            if foreign_key := data.get("foreign_key", None):
                sql_column.is_foreign = foreign_key == "YES"

            if data.get("character_maximum_length", None) is not None:
                sql_column.max_length = data.get("character_maximum_length", 0)

            if data.get("numeric_precision", None) is not None:
                sql_column.precision = data.get("numeric_precision", 0)

            if data.get("numeric_scale", None) is not None:
                sql_column.numeric_scale = data.get("numeric_scale", 0)

            if data.get("table_type") == "MATERIALIZED VIEW" or is_materialized:
                sql_column.attributes.view_name = json.dumps(data["table_name"])
                sql_column.attributes.view_qualified_name = json.dumps(
                    f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}/{data['table_name']}"
                )
                sql_column.attributes.materialised_view = MaterialisedView.creator(
                    name=json.dumps(data["table_name"]),
                    schema_qualified_name=f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}",
                )
            elif data.get("table_type") == "VIEW":
                sql_column.attributes.view_name = json.dumps(data["table_name"])
                sql_column.attributes.view_qualified_name = json.dumps(
                    f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}/{data['table_name']}"
                )
                sql_column.attributes.view = View.creator(
                    name=json.dumps(data["table_name"]),
                    schema_qualified_name=f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}",
                )
            elif (
                data.get("table_type") in ("DYNAMIC TABLE", "DYNAMIC_TABLE")
                or data.get("is_dynamic") == "YES"
            ):
                sql_column.attributes.table_name = json.dumps(data["table_name"])
                sql_column.attributes.table_qualified_name = json.dumps(
                    f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}/{data['table_name']}"
                )
                sql_column.attributes.snowflake_dynamic_table = SnowflakeDynamicTable.creator(
                    name=json.dumps(data["table_name"]),
                    schema_qualified_name=f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}",
                )
            else:
                sql_column.attributes.table_name = json.dumps(data["table_name"])
                sql_column.attributes.table_qualified_name = json.dumps(
                    f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}/{data['table_name']}"
                )
                sql_column.attributes.table = Table.creator(
                    name=json.dumps(data["table_name"]),
                    schema_qualified_name=f"{base_qualified_name}/{data['table_cat']}/{data['table_schem']}",
                )

            if not sql_column.custom_attributes:
                sql_column.custom_attributes = {}

            sql_column.custom_attributes["is_self_referencing"] = data.get(
                "is_self_referencing", "NO"
            )

            if data.get("column_id", None):
                sql_column.custom_attributes["source_id"] = data.get("column_id")
                sql_column.custom_attributes["catalog_id"] = data.get(
                    "table_catalog_id"
                )
                sql_column.custom_attributes["schema_id"] = data.get("table_schema_id")
                sql_column.custom_attributes["table_id"] = data.get("table_id")

            sql_column.custom_attributes["character_octet_length"] = data.get(
                "character_octet_length", None
            )
            sql_column.custom_attributes["is_auto_increment"] = data.get(
                "is_autoincrement"
            )
            sql_column.custom_attributes["is_generated"] = data.get(
                "is_generatedcolumn"
            )
            sql_column.custom_attributes["extra_info"] = data.get("extra_info", None)
            sql_column.custom_attributes["buffer_length"] = data.get("buffer_length")
            sql_column.custom_attributes["column_size"] = data.get("column_size")

            sql_column.last_sync_run_at = datetime.now()

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
                name=json.dumps(data["pipe_schema"]),
                database_qualified_name=f"{base_qualified_name}/{data['pipe_catalog']}",
            )

            snowflake_pipe.last_sync_run_at = datetime.now()
            # TODO:
            # snowflake_pipe.last_sync_run = last_sync_run

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
            assert (
                data["function_catalog"] is not None
            ), "Function catalog cannot be None"
            assert data["function_schema"] is not None, "Function schema cannot be None"

            # TODO:
            # "lastSyncWorkflowName": {{external_map['crawler_name'] | tojson}},
            # "lastSyncRun": {{external_map['workflow_name'] | tojson}},
            # "tenantId": {{external_map['tenant_id'] | tojson}},

            data_type = data.get("data_type", "")
            function_type = "Scalar"
            if "table" in data_type:
                function_type = "TABLE"

            # TODO: Creator has not been implemented yet
            function = Function.create(
                name=json.dumps(data["function_name"]),
                function_arguments=data["argument_signature"][1:-1].split(", "),
                function_definition=data["function_definition"],
                function_language=data["function_language"],
                function_return_type=json.dumps(data_type),
                function_type=function_type,
                database_qualified_name=f"{base_qualified_name}/{data['function_catalog']}",
                schema_qualified_name=f"{base_qualified_name}/{data['function_catalog']}/{data['function_schema']}",
                connection_qualified_name=f"{base_qualified_name}/{data['function_catalog']}/{data['function_schema']}",
            )
            function.database_name = json.dumps(data["function_catalog"])
            function.schema_name = json.dumps(data["function_schema"])

            if data.get("is_secure", None) is not None:
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

            if data.get("function_owner", None) is not None:
                function.attributes.source_created_by = data.get("function_owner")

            if data.get("created", None) is not None:
                function.attributes.source_created_at = datetime.strptime(
                    data.get("created"), "%Y-%m-%dT%H:%M:%S.%f%z"
                )

            if data.get("last_altered", None) is not None:
                function.attributes.source_updated_at = datetime.strptime(
                    data.get("last_altered"), "%Y-%m-%dT%H:%M:%S.%f%z"
                )

            function.last_sync_run_at = datetime.now()

            function.attributes.atlan_schema = Schema.creator(
                name=json.dumps(data["function_schema"]),
                database_qualified_name=f"{base_qualified_name}/{data['function_catalog']}",
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
                source_updated_at=datetime.strptime(
                    data["last_altered"], "%Y-%m-%dT%H:%M:%S.%f%z"
                ),
            )

            tag.attributes.atlan_schema = Schema.creator(
                name=json.dumps(data["tag_schema"]),
                database_qualified_name=f"{base_qualified_name}/{data['tag_database']}",
            )

            tag.last_sync_run_at = datetime.now()
            # TODO:
            # tag.last_sync_run = last_sync_run

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
                name=json.dumps(data["tag_name"]),
                tag_attachment_string_value=data["tag_value"],
            )

            tag_attachment.last_sync_run_at = datetime.now()
            # TODO:
            # tag_attachment.last_sync_run = last_sync_run

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
                name=json.dumps(data["name"]),
                stream_type=data["type"],
                stream_source_type=data["source_type"],
                stream_mode=data["mode"],
                stream_is_stale=data["stale"],
                stream_stale_after=data["stale_after"],
            )

            snowflake_stream.attributes.atlan_schema = Schema.creator(
                name=json.dumps(data["schema_name"]),
                database_qualified_name=f"{base_qualified_name}/{data['database_name']}",
            )

            snowflake_stream.last_sync_run_at = datetime.now()
            # TODO:
            # snowflake_stream.last_sync_run = last_sync_run

            return snowflake_stream
        except AssertionError as e:
            logger.error(f"Error creating ColumnEntity: {str(e)}")
            return None
