import logging
from typing import Any, Dict, Union

from pyatlan.model import assets

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
                database_qualified_name=f"{obj['connection_qualified_name']}/{obj['catalog_name']}",
                database_name=obj["catalog_name"],
                connection_qualified_name=obj["connection_qualified_name"],
            )
            schema.attributes.table_count = obj.get("table_count", 0)
            schema.attributes.views_count = obj.get("views_count", 0)
            return schema
        except AssertionError as e:
            raise ValueError(f"Error creating Schema Entity: {str(e)}")


class Table(assets.Table):
    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> Union[assets.Table, assets.View]:
        try:
            assert obj.get("table_name") is not None, "Table name cannot be None"
            assert obj.get("table_catalog") is not None, "Table catalog cannot be None"
            assert obj.get("table_schema") is not None, "Table schema cannot be None"

            table_type = (
                assets.Table
                if obj.get("table_type") in ["TABLE", "BASE TABLE"]
                else assets.View
            )

            sql_table = table_type.creator(
                name=obj["table_name"],
                schema_qualified_name=f"{obj['connection_qualified_name']}/{obj['table_catalog']}/{obj['table_schema']}",
                schema_name=obj["table_schema"],
                database_name=obj["table_catalog"],
                database_qualified_name=f"{obj['connection_qualified_name']}/{obj['table_catalog']}",
                connection_qualified_name=obj["connection_qualified_name"],
            )

            if table_type == assets.View:
                sql_table.attributes.definition = obj.get("VIEW_DEFINITION", "")

            sql_table.attributes.column_count = obj.get("column_count", 0)
            sql_table.attributes.row_count = obj.get("row_count", 0)
            sql_table.attributes.size_bytes = obj.get("size_bytes", 0)
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
                else assets.View
            )

            sql_column = assets.Column.creator(
                name=obj["column_name"],
                parent_qualified_name=f"{obj['connection_qualified_name']}/{obj['table_catalog']}/{obj['table_schema']}/{obj['table_name']}",
                parent_type=parent_type,
                order=obj["ordinal_position"],
                parent_name=obj["table_name"],
                database_name=obj["table_catalog"],
                database_qualified_name=f"{obj['connection_qualified_name']}/{obj['table_catalog']}",
                schema_name=obj["table_schema"],
                schema_qualified_name=f"{obj['connection_qualified_name']}/{obj['table_catalog']}/{obj['table_schema']}",
                table_name=obj["table_name"],
                table_qualified_name=f"{obj['connection_qualified_name']}/{obj['table_catalog']}/{obj['table_schema']}/{obj['table_name']}",
                connection_qualified_name=obj["connection_qualified_name"],
            )
            sql_column.attributes.data_type = obj.get("data_type", "")
            sql_column.attributes.is_nullable = obj.get("is_nullable", "YES") == "YES"

            return sql_column
        except AssertionError as e:
            raise ValueError(f"Error creating Column Entity: {str(e)}")
