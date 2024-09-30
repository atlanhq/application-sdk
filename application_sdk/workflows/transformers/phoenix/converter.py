import logging
from typing import Any, Dict, Optional

from application_sdk.workflows.transformers.phoenix.schema import (
    BaseObjectEntity,
    ColumnConstraint,
    ColumnEntity,
    DatabaseEntity,
    Namespace,
    Package,
    SchemaEntity,
    TableEntity,
    ViewEntity,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def transform_metadata(
    connector_name: str, connector_type: str, typename: str, data: Dict[str, Any]
) -> Optional[BaseObjectEntity]:
    """Transform metadata to Atlan Open Spec. 

    :param connector_name: The name of the connector.
    :param connector_type: The type of the connector.
    :param typename: The type of the object.
    :param data: The data to transform.
    :return: The transformed object.
    """
    connector_temp = f"{connector_name}_{connector_type}"
    namespace = Namespace(
        id=connector_temp,
        name=connector_temp,
    )
    package = Package(
        id=connector_temp,
        name=connector_temp,
    )

    if typename.upper() == "DATABASE":
        try:
            assert data["datname"] is not None, "Database name cannot be None"
            return DatabaseEntity(
                namespace=namespace,
                package=package,
                typeName="DATABASE",
                name=data["datname"],
                URI=f"/{connector_name}/{connector_type}/{data['datname']}",
            )
        except AssertionError as e:
            logger.error(f"Error creating DatabaseEntity: {str(e)}")
            return None

    elif typename.upper() == "SCHEMA":
        try:
            assert data["schema_name"] is not None, "Schema name cannot be None"
            assert data["catalog_name"] is not None, "Catalog name cannot be None"
            return SchemaEntity(
                namespace=namespace,
                package=package,
                typeName="SCHEMA",
                name=data["schema_name"],
                URI=f"/{connector_name}/{connector_type}/{data['catalog_name']}/{data['schema_name']}",
            )
        except AssertionError as e:
            logger.error(f"Error creating SchemaEntity: {str(e)}")
            return None

    elif typename.upper() == "TABLE":
        try:
            assert data["table_name"] is not None, "Table name cannot be None"
            assert data["table_cat"] is not None, "Table catalog cannot be None"
            assert data["table_schem"] is not None, "Table schema cannot be None"

            if data.get("table_type") == "VIEW":
                return ViewEntity(
                    namespace=namespace,
                    package=package,
                    typeName="VIEW",
                    name=data["table_name"],
                    URI=f"/{connector_name}/{connector_type}/{data['table_cat']}/{data['table_schem']}/{data['table_name']}",
                )
            else:
                return TableEntity(
                    namespace=namespace,
                    package=package,
                    typeName="TABLE",
                    name=data["table_name"],
                    URI=f"/{connector_name}/{connector_type}/{data['table_cat']}/{data['table_schem']}/{data['table_name']}",
                    isPartition=data.get("is_partition") or False,
                )
        except AssertionError as e:
            logger.error(f"Error creating TableEntity: {str(e)}")
            return None

    elif typename.upper() == "COLUMN":
        try:
            assert data["column_name"] is not None, "Column name cannot be None"
            assert data["table_cat"] is not None, "Table catalog cannot be None"
            assert data["table_schem"] is not None, "Table schema cannot be None"
            assert data["table_name"] is not None, "Table name cannot be None"
            assert (
                data["ordinal_position"] is not None
            ), "Ordinal position cannot be None"
            assert data["data_type"] is not None, "Data type cannot be None"

            return ColumnEntity(
                namespace=namespace,
                package=package,
                typeName="COLUMN",
                name=data["column_name"],
                URI=f"/{connector_name}/{connector_type}/{data['table_cat']}/{data['table_schem']}/{data['table_name']}/{data['column_name']}",
                order=data["ordinal_position"],
                dataType=data["data_type"],
                constraints=ColumnConstraint(
                    notNull=not data.get("is_nullable") == "NO",
                    autoIncrement=data.get("is_autoincrement") == "YES",
                ),
            )
        except AssertionError as e:
            logger.error(f"Error creating ColumnEntity: {str(e)}")
            return None

    else:
        logger.error(f"Unknown typename: {typename}")
        name = data[f"{typename.lower()}_name"]
        return BaseObjectEntity(
            typeName=typename.upper(),
            name=name,
            URI=f"/{connector_name}/{connector_type}/{name}",
        )
