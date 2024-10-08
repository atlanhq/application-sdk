import logging
from typing import Any, Dict, Optional, Union

from pyatlan.model.assets import Column, Database, Schema, Table, View
from pydantic.v1 import BaseModel

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
        >>>     def transform_metadata(self, connector_name: str, typename: str, data: Dict[str, Any], **kwargs: Any) -> Optional[BaseModel]:
        >>>         # Custom logic here
    """

    def __init__(self, **kwargs: Any):
        self.timestamp = kwargs.get("timestamp", "0")

    def transform_metadata(
        self, connector_name: str, typename: str, data: Dict[str, Any], **kwargs: Any
    ) -> Optional[BaseModel]:
        base_qualified_name = kwargs.get(
            "base_qualified_name", f"default/{connector_name}/{self.timestamp}"
        )

        entity_creators = {
            "DATABASE": self._create_database_entity,
            "SCHEMA": self._create_schema_entity,
            "TABLE": self._create_table_entity,
            "COLUMN": self._create_column_entity,
        }

        creator = entity_creators.get(typename.upper())
        if creator:
            return creator(data, base_qualified_name)
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

            if data.get("table_type") == "VIEW":
                sql_table = View.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )
                sql_table.attributes.definition = data.get("VIEW_DEFINITION", "")
            else:
                sql_table = Table.creator(
                    name=data["table_name"],
                    schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                )
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
