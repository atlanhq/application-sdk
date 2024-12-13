import json
import logging
from typing import Any, Dict, List, Optional, Union, overload

from pyatlan.model import assets
from pyatlan.model.enums import AtlanConnectorType
from pyatlan.utils import init_guid, validate_required_fields

from application_sdk.workflows.transformers.utils import build_atlas_qualified_name

logger = logging.getLogger(__name__)


class Database(assets.Database):
    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> assets.Database:
        try:
            assert obj.get("database_name") is not None and isinstance(
                obj.get("database_name"), str
            ), "Database name cannot be None"
            assert obj.get("connection_qualified_name") is not None and isinstance(
                obj.get("connection_qualified_name"), str
            ), "Connection qualified name cannot be None"

            database = assets.Database.creator(
                name=obj["database_name"],
                connection_qualified_name=obj["connection_qualified_name"],
            )

            database.attributes.schema_count = obj.get("schema_count", 0)

            return database
        except AssertionError as e:
            raise ValueError(f"Error creating Database Entity: {str(e)}")


class Schema(assets.Schema):
    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> assets.Schema:
        try:
            assert obj.get("schema_name") is not None and isinstance(
                obj.get("schema_name"), str
            ), "Schema name cannot be None"
            assert obj.get("connection_qualified_name") is not None and isinstance(
                obj.get("connection_qualified_name"), str
            ), "Connection qualified name cannot be None"

            schema = assets.Schema.creator(
                name=obj["schema_name"],
                database_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"], obj["catalog_name"]
                ),
                database_name=obj["catalog_name"],
                connection_qualified_name=obj["connection_qualified_name"],
            )

            schema.attributes.table_count = obj.get("table_count", 0)
            schema.attributes.views_count = obj.get("views_count", 0)

            # Q: Can we use the `Attributes` class directly here?

            if not schema.custom_attributes:
                schema.custom_attributes = {}

            if catalog_id := obj.get("catalog_id", None):
                schema.custom_attributes["catalog_id"] = catalog_id

            if is_managed_access := obj.get("is_managed_access", None):
                schema.custom_attributes["is_managed_access"] = is_managed_access

            return schema
        except AssertionError as e:
            raise ValueError(f"Error creating Schema Entity: {str(e)}")


class Table(assets.Table):
    @classmethod
    def parse_obj(
        cls, obj: Dict[str, Any]
    ) -> Union[
        assets.Table, assets.View, assets.MaterialisedView, assets.SnowflakeDynamicTable
    ]:
        try:
            assert obj.get("table_name") is not None, "Table name cannot be None"
            assert obj.get("table_catalog") is not None, "Table catalog cannot be None"
            assert obj.get("table_schema") is not None, "Table schema cannot be None"

            table_type = (
                assets.Table
                if obj.get("table_type") in ["TABLE", "BASE TABLE"]
                else assets.MaterialisedView
                if obj.get("table_type") == "MATERIALIZED VIEW"
                else assets.SnowflakeDynamicTable
                if obj.get("table_type") == "DYNAMIC TABLE"
                or obj.get("is_dynamic") == "YES"
                else assets.View
            )

            sql_table = table_type.creator(
                name=obj["table_name"],
                schema_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["table_catalog"],
                    obj["table_schema"],
                ),
                schema_name=obj["table_schema"],
                database_name=obj["table_catalog"],
                database_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"], obj["table_catalog"]
                ),
                connection_qualified_name=obj["connection_qualified_name"],
            )

            if table_type in [assets.View, assets.MaterialisedView]:
                sql_table.attributes.definition = obj.get("VIEW_DEFINITION", "")

            sql_table.attributes.column_count = obj.get("column_count", 0)
            sql_table.attributes.row_count = obj.get("row_count", 0)
            sql_table.attributes.size_bytes = obj.get("size_bytes", 0)

            # Custom attributes
            if not sql_table.custom_attributes:
                sql_table.custom_attributes = {}

            sql_table.custom_attributes["is_transient"] = obj.get("is_transient")

            sql_table.custom_attributes["catalog_id"] = obj.get("table_catalog_id")
            sql_table.custom_attributes["schema_id"] = obj.get("table_schema_id")

            sql_table.custom_attributes["last_ddl"] = obj.get("last_ddl")
            sql_table.custom_attributes["last_ddl_by"] = obj.get("last_ddl_by")

            sql_table.custom_attributes["is_secure"] = obj.get("is_secure")
            sql_table.custom_attributes["retention_time"] = obj.get("retention_time")
            sql_table.custom_attributes["stage_url"] = obj.get("stage_url")
            sql_table.custom_attributes["is_insertable_into"] = obj.get(
                "is_insertable_into"
            )
            sql_table.custom_attributes["number_columns_in_part_key"] = obj.get(
                "number_columns_in_part_key"
            )
            sql_table.custom_attributes["columns_participating_in_part_key"] = obj.get(
                "columns_participating_in_part_key"
            )
            sql_table.custom_attributes["is_typed"] = obj.get("is_typed")
            sql_table.custom_attributes["auto_clustering_on"] = obj.get(
                "auto_clustering_on"
            )
            sql_table.custom_attributes["engine"] = obj.get("engine")
            sql_table.custom_attributes["auto_increment"] = obj.get("auto_increment")

            return sql_table
        except AssertionError as e:
            raise ValueError(f"Error creating Table Entity: {str(e)}")


class Column(assets.Column):
    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> assets.Column:
        try:
            assert obj.get("column_name") is not None, "Column name cannot be None"
            assert obj.get("table_catalog") is not None, "Table catalog cannot be None"
            assert obj.get("table_schema") is not None, "Table schema cannot be None"
            assert obj.get("table_name") is not None, "Table name cannot be None"
            assert (
                obj.get("ordinal_position") is not None
            ), "Ordinal position cannot be None"
            assert obj.get("data_type") is not None, "Data type cannot be None"

            parent_type = (
                assets.Table
                if obj.get("table_type") in ["TABLE", "BASE TABLE"]
                else assets.MaterialisedView
                if obj.get("table_type") == "MATERIALIZED VIEW"
                else assets.SnowflakeDynamicTable
                if obj.get("table_type") == "DYNAMIC TABLE"
                or obj.get("is_dynamic") == "YES"
                else assets.View
            )

            sql_column = assets.Column.creator(
                name=obj["column_name"],
                parent_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["table_catalog"],
                    obj["table_schema"],
                    obj["table_name"],
                ),
                parent_type=parent_type,
                order=obj.get(
                    "ordinal_position",
                    obj.get("column_id", obj.get("internal_column_id", "")),
                ),
                parent_name=obj["table_name"],
                database_name=obj["table_catalog"],
                database_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"], obj["table_catalog"]
                ),
                schema_name=obj["table_schema"],
                schema_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["table_catalog"],
                    obj["table_schema"],
                ),
                table_name=obj["table_name"],
                table_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["table_catalog"],
                    obj["table_schema"],
                    obj["table_name"],
                ),
                connection_qualified_name=obj["connection_qualified_name"],
            )
            sql_column.attributes.data_type = obj.get("data_type")
            sql_column.attributes.is_nullable = obj.get("is_nullable", "YES") == "YES"
            sql_column.attributes.is_partition = obj.get("is_partition", None) == "YES"
            sql_column.attributes.partition_order = obj.get("partition_order", 0)
            sql_column.attributes.is_primary = obj.get("primary_key", None) == "YES"
            sql_column.attributes.is_foreign = obj.get("foreign_key", None) == "YES"
            sql_column.attributes.max_length = obj.get("character_maximum_length", 0)
            sql_column.attributes.precision = obj.get("numeric_precision", 0)
            sql_column.attributes.numeric_scale = obj.get("numeric_scale", 0)

            if not sql_column.custom_attributes:
                sql_column.custom_attributes = {}

            sql_column.custom_attributes["is_self_referencing"] = obj.get(
                "is_self_referencing", "NO"
            )

            sql_column.custom_attributes["catalog_id"] = obj.get("table_catalog_id")
            sql_column.custom_attributes["schema_id"] = obj.get("table_schema_id")
            sql_column.custom_attributes["table_id"] = obj.get("table_id")

            sql_column.custom_attributes["character_octet_length"] = obj.get(
                "character_octet_length", None
            )
            sql_column.custom_attributes["is_auto_increment"] = obj.get(
                "is_autoincrement"
            )
            sql_column.custom_attributes["is_generated"] = obj.get("is_generatedcolumn")
            sql_column.custom_attributes["extra_info"] = obj.get("extra_info", None)
            sql_column.custom_attributes["buffer_length"] = obj.get("buffer_length")
            sql_column.custom_attributes["column_size"] = obj.get("column_size")

            return sql_column
        except AssertionError as e:
            raise ValueError(f"Error creating Column Entity: {str(e)}")


class Function(assets.Function):
    @overload
    @classmethod
    def creator(
        cls,
        *,
        name: str,
    ) -> "Function": ...

    @overload
    @classmethod
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: str,
        database_name: str,
        database_qualified_name: str,
        connection_qualified_name: str,
    ) -> "Function": ...

    @classmethod
    @init_guid
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: Optional[str] = None,
        database_name: Optional[str] = None,
        database_qualified_name: Optional[str] = None,
        connection_qualified_name: Optional[str] = None,
    ) -> "Function":
        validate_required_fields(
            ["name", "schema_qualified_name"], [name, schema_qualified_name]
        )
        attributes = Function.Attributes.create(
            name=name,
            schema_qualified_name=schema_qualified_name,
            schema_name=schema_name,
            database_name=database_name,
            database_qualified_name=database_qualified_name,
            connection_qualified_name=connection_qualified_name,
        )
        return cls(attributes=attributes)

    class Attributes(assets.Function.Attributes):
        function_arguments: List[str] | None = []

        @classmethod
        @init_guid
        def create(
            cls,
            *,
            name: str,
            schema_qualified_name: str,
            schema_name: Optional[str] = None,
            database_name: Optional[str] = None,
            database_qualified_name: Optional[str] = None,
            connection_qualified_name: Optional[str] = None,
        ) -> "Function.Attributes":
            validate_required_fields(
                ["name, schema_qualified_name"], [name, schema_qualified_name]
            )
            if connection_qualified_name:
                connector_name = AtlanConnectorType.get_connector_name(
                    connection_qualified_name
                )
            else:
                connection_qn, connector_name = AtlanConnectorType.get_connector_name(
                    schema_qualified_name, "schema_qualified_name", 5
                )

            fields = schema_qualified_name.split("/")
            qualified_name = f"{schema_qualified_name}/{name}"
            connection_qualified_name = connection_qualified_name or connection_qn
            database_name = database_name or fields[3]
            schema_name = schema_name or fields[4]
            database_qualified_name = (
                database_qualified_name
                or f"{connection_qualified_name}/{database_name}"
            )
            function_schema = Schema.ref_by_qualified_name(schema_qualified_name)

            return Function.Attributes(
                name=name,
                qualified_name=qualified_name,
                database_name=database_name,
                database_qualified_name=database_qualified_name,
                schema_name=schema_name,
                schema_qualified_name=schema_qualified_name,
                connector_name=connector_name,
                connection_qualified_name=connection_qualified_name,
                function_schema=function_schema,
            )

    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> assets.Function:
        try:
            assert (
                "function_name" in obj and obj["function_name"] is not None
            ), "Function name cannot be None"
            assert (
                "argument_signature" in obj and obj["argument_signature"] is not None
            ), "Function argument signature cannot be None"
            assert (
                "function_definition" in obj and obj["function_definition"] is not None
            ), "Function definition cannot be None"
            assert (
                "is_external" in obj and obj["is_external"] is not None
            ), "Function is_external name cannot be None"
            assert (
                "is_memoizable" in obj and obj["is_memoizable"] is not None
            ), "Function is_memoizable cannot be None"
            assert (
                "function_language" in obj and obj["function_language"] is not None
            ), "Function language cannot be None"
            assert (
                "function_catalog" in obj and obj["function_catalog"] is not None
            ), "Function catalog cannot be None"
            assert (
                "function_schema" in obj and obj["function_schema"] is not None
            ), "Function schema cannot be None"

            function = Function.creator(
                name=obj["function_name"],
                database_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"], obj["function_catalog"]
                ),
                schema_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["function_catalog"],
                    obj["function_schema"],
                ),
                connection_qualified_name=obj["connection_qualified_name"],
                schema_name=obj["function_schema"],
                database_name=obj["function_catalog"],
            )
            if "TABLE" in obj.get("data_type", None):
                function.attributes.function_type = "Tabular"
            else:
                function.attributes.function_type = "Scalar"
            function.attributes.function_return_type = obj.get("data_type", None)
            function.attributes.function_language = obj.get("function_language", None)
            function.attributes.function_definition = obj.get(
                "function_definition", None
            )
            function.attributes.function_arguments = list(
                obj.get("argument_signature", "()")[1:-1].split(",")
            )
            function.attributes.function_is_secure = obj.get("is_secure", None) == "YES"
            function.attributes.function_is_external = (
                obj.get("is_external", None) == "YES"
            )
            function.attributes.function_is_d_m_f = (
                obj.get("is_data_metric", None) == "YES"
            )
            function.attributes.function_is_memoizable = (
                obj.get("is_memoizable", None) == "YES"
            )

            return function
        except AssertionError as e:
            raise ValueError(f"Error creating Function Entity: {str(e)}")


class TagAttachment(assets.TagAttachment):
    @overload
    @classmethod
    def creator(
        cls,
        *,
        name: str,
    ) -> "TagAttachment": ...

    @overload
    @classmethod
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: str,
        database_name: str,
        database_qualified_name: str,
        connection_qualified_name: str,
    ) -> "TagAttachment": ...

    @classmethod
    @init_guid
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: Optional[str] = None,
        database_name: Optional[str] = None,
        database_qualified_name: Optional[str] = None,
        connection_qualified_name: Optional[str] = None,
    ) -> "TagAttachment":
        validate_required_fields(
            ["name", "schema_qualified_name"], [name, schema_qualified_name]
        )
        attributes = TagAttachment.Attributes.create(
            name=name,
            schema_qualified_name=schema_qualified_name,
            schema_name=schema_name,
            database_name=database_name,
            database_qualified_name=database_qualified_name,
            connection_qualified_name=connection_qualified_name,
        )
        return cls(attributes=attributes)

    class Attributes(assets.TagAttachment.Attributes):
        @classmethod
        @init_guid
        def create(
            cls,
            *,
            name: str,
            schema_qualified_name: str,
            schema_name: Optional[str] = None,
            database_name: Optional[str] = None,
            database_qualified_name: Optional[str] = None,
            connection_qualified_name: Optional[str] = None,
        ) -> "TagAttachment.Attributes":
            validate_required_fields(
                ["name, schema_qualified_name"], [name, schema_qualified_name]
            )
            if connection_qualified_name:
                connector_name = AtlanConnectorType.get_connector_name(
                    connection_qualified_name
                )
            else:
                connection_qn, connector_name = AtlanConnectorType.get_connector_name(
                    schema_qualified_name, "schema_qualified_name", 5
                )

            fields = schema_qualified_name.split("/")
            qualified_name = f"{schema_qualified_name}/{name}"
            connection_qualified_name = connection_qualified_name or connection_qn
            database_name = database_name or fields[3]
            schema_name = schema_name or fields[4]
            database_qualified_name = (
                database_qualified_name
                or f"{connection_qualified_name}/{database_name}"
            )

            return TagAttachment.Attributes(
                name=name,
                qualified_name=qualified_name,
                database_name=database_name,
                database_qualified_name=database_qualified_name,
                schema_name=schema_name,
                schema_qualified_name=schema_qualified_name,
                connector_name=connector_name,
                connection_qualified_name=connection_qualified_name,
            )

    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> assets.TagAttachment:
        try:
            assert (
                "tag_name" in obj and obj["tag_name"] is not None
            ), "Tag name cannot be None"
            assert (
                "tag_database" in obj and obj["tag_database"] is not None
            ), "Tag database cannot be None"
            assert (
                "tag_schema" in obj and obj["tag_schema"] is not None
            ), "Tag schema cannot be None"
            assert (
                "object_database" in obj and obj["object_database"] is not None
            ), "Object database cannot be None"
            assert (
                "object_schema" in obj and obj["object_schema"] is not None
            ), "Object schema cannot be None"

            tag_attachment = TagAttachment.create(
                name=obj["tag_name"],
                connection_qualified_name=obj["connection_qualified_name"],
                database_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"], obj["tag_database"]
                ),
                schema_qualified_name=build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["tag_database"],
                    obj["tag_schema"],
                ),
            )
            tag_attachment.tag_qualified_name = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["tag_database"],
                obj["tag_schema"],
                obj["tag_name"],
            )
            object_cat = obj.get("object_database", "")
            object_schema = obj.get("object_schema", "")

            tag_attachment.attributes.object_database_qualified_name = (
                build_atlas_qualified_name(obj["connection_qualified_name"], object_cat)
            )
            tag_attachment.attributes.object_schema_qualified_name = (
                build_atlas_qualified_name(
                    obj["connection_qualified_name"], object_cat, object_schema
                )
            )
            tag_attachment.attributes.object_database_name = object_cat
            tag_attachment.attributes.object_schema_name = object_schema
            tag_attachment.attributes.object_domain = obj.get("domain", None)
            tag_attachment.attributes.object_name = obj.get("object_name", None)
            tag_attachment.attributes.database_name = obj["tag_database"]
            tag_attachment.attributes.schema_name = obj["tag_schema"]
            tag_attachment.attributes.source_tag_id = obj.get("tag_id", None)
            tag_attachment.attributes.tag_attachment_string_value = obj.get(
                "tag_value", None
            )

            if object_domain := obj.get("domain", None):
                object_cat = obj.get("object_cat", "")
                object_schema = obj.get("object_schema", "")
                object_name = obj.get("object_name", "")
                column_name = obj.get("column_name", "")

                object_qualified_name = ""
                if object_domain == "DATABASE":
                    object_qualified_name = build_atlas_qualified_name(
                        obj["connection_qualified_name"], object_cat, object_name
                    )
                elif object_domain == "SCHEMA":
                    object_qualified_name = build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        object_cat,
                        object_schema,
                        object_name,
                    )
                elif object_domain in ["TABLE", "STREAM", "PIPE"]:
                    object_qualified_name = build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        object_cat,
                        object_schema,
                        object_name,
                    )
                elif object_domain == "COLUMN":
                    object_qualified_name = build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        object_cat,
                        object_schema,
                        object_name,
                        column_name,
                    )

                tag_attachment.attributes.object_qualified_name = object_qualified_name

            if classification_defs := obj.get("classification_defs", []):
                tag_name = obj.get("tag_name", "").upper()
                matching_defs = [
                    c
                    for c in classification_defs
                    if c.get("displayName", "").upper() == tag_name
                ]

                if matching_defs:
                    oldest_def = min(
                        matching_defs, key=lambda x: x.get("createTime", float("inf"))
                    )
                    tag_attachment.mapped_classification_name = json.dumps(
                        oldest_def.get("name")
                    )
                else:
                    tag_attachment.mapped_classification_name = json.dumps(
                        obj.get("mappedClassificationName", "")
                    )
            else:
                tag_attachment.mapped_classification_name = json.dumps(
                    obj.get("mappedClassificationName", "")
                )

            return tag_attachment
        except Exception as e:
            raise ValueError(f"Error creating TagAttachment Entity: {str(e)}")
