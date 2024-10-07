import logging
from typing import Any, Dict, Optional

from pyatlan.model.assets import Column, Database, Schema, Table, View
from pydantic.v1 import BaseModel

from application_sdk.workflows.transformers import TransformerInterface

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AtlasTransformer(TransformerInterface):
    def __init__(self, **kwargs: Any):
        self.timestamp = kwargs.get("timestamp", "0")

    def transform_metadata(
        self, connector_name: str, typename: str, data: Dict[str, Any], **kwargs: Any
    ) -> Optional[BaseModel]:
        base_qualified_name = kwargs.get(
            "base_qualified_name", f"default/{connector_name}/{self.timestamp}"
        )

        if typename.upper() == "DATABASE":
            try:
                assert data["datname"] is not None, "Database name cannot be None"
                return Database.creator(
                    name=data["datname"],
                    connection_qualified_name=f"{base_qualified_name}",
                )
            except AssertionError as e:
                logger.error(f"Error creating DatabaseEntity: {str(e)}")
                return None

        elif typename.upper() == "SCHEMA":
            try:
                assert data["schema_name"] is not None, "Schema name cannot be None"
                assert data["catalog_name"] is not None, "Catalog name cannot be None"
                return Schema.creator(
                    name=data["schema_name"],
                    database_qualified_name=f"{base_qualified_name}/{data['catalog_name']}",
                )
            except AssertionError as e:
                logger.error(f"Error creating SchemaEntity: {str(e)}")
                return None

        elif typename.upper() == "TABLE":
            try:
                assert data["table_name"] is not None, "Table name cannot be None"
                assert data["table_catalog"] is not None, "Table catalog cannot be None"
                assert data["table_schema"] is not None, "Table schema cannot be None"

                if data.get("table_type") == "VIEW":
                    return View.creator(
                        name=data["table_name"],
                        schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                    )
                else:
                    return Table.create(
                        name=data["table_name"],
                        schema_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}",
                    )
            except AssertionError as e:
                logger.error(f"Error creating TableEntity: {str(e)}")
                return None

        elif typename.upper() == "COLUMN":
            try:
                assert data["column_name"] is not None, "Column name cannot be None"
                assert data["table_catalog"] is not None, "Table catalog cannot be None"
                assert data["table_schema"] is not None, "Table schema cannot be None"
                assert data["table_name"] is not None, "Table name cannot be None"
                assert (
                    data["ordinal_position"] is not None
                ), "Ordinal position cannot be None"
                assert data["data_type"] is not None, "Data type cannot be None"

                return Column.create(
                    name=data["column_name"],
                    parent_qualified_name=f"{base_qualified_name}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}",
                    parent_type=Table,
                    order=data["ordinal_position"],
                    # dataType=data["data_type"],
                    # constraints=ColumnConstraint(
                    #     notNull=not data.get("is_nullable") == "NO",
                    #     autoIncrement=data.get("is_autoincrement") == "YES",
                    # ),
                )
            except AssertionError as e:
                logger.error(f"Error creating ColumnEntity: {str(e)}")
                return None

        else:
            logger.error(f"Unknown typename: {typename}")
            return None
            # return BaseObjectEntity(
            #     typeName=typename.upper(),
            #     name=name,
            #     URI=f"/{connector_name}/{connector_type}/{name}",
            # )
