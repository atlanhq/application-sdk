import logging
from typing import Any, Dict, Optional

from pydantic.v1 import BaseModel

from application_sdk.workflows.transformers import TransformerInterface
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


class PhoenixTransformer(TransformerInterface):
    """
    PhoenixTransformer is a class that transforms metadata into Phoenix entities.
    It uses the pyatlan library to create the entities.

    Attributes:
        timestamp (str): The timestamp of the metadata.

    Usage:
        Subclass this class and override the transform_metadata method to customize the transformation process.
        Then use the subclass as an argument to the SQLWorkflowWorker.

        >>> class CustomPhoenixTransformer(PhoenixTransformer):
        >>>     def transform_metadata(self, connector_name: str, typename: str, data: Dict[str, Any], **kwargs: Any) -> Optional[BaseModel]:
        >>>         # Custom logic here
    """

    def __init__(self, **kwargs: Any):
        pass

    def transform_metadata(
        self, connector_name: str, typename: str, data: Dict[str, Any], **kwargs: Any
    ) -> Optional[BaseModel]:
        """Transform metadata to Atlan Open Spec.

        :param connector_name: The name of the connector.
        :param typename: The type of the object.
        :param data: The data to transform.
        :return: The transformed object.

        Usage:
            >>> transform_metadata("redshift", "sql", "table", {"table_name": "instacart_orders", "table_schema": "public", "table_catalog": "dev"})
        """
        type_name = typename.upper()
        connector_type = kwargs.get("connector_type", "phoenix")
        connector_temp = f"{connector_name}-{connector_type}"
        namespace = Namespace(
            id=connector_temp,
            name=connector_temp,
        )
        package = Package(
            id=connector_temp,
            name=connector_temp,
        )

        if type_name == "DATABASE":
            try:
                assert data["datname"] is not None, "Database name cannot be None"
                return DatabaseEntity(
                    namespace=namespace,
                    package=package,
                    typeName=type_name,
                    name=data["datname"],
                    URI=f"/{connector_name}/{connector_type}/{data['datname']}",
                )
            except AssertionError as e:
                logger.error(f"Error creating DatabaseEntity: {str(e)}")
                return None

        elif type_name == "SCHEMA":
            try:
                assert data["schema_name"] is not None, "Schema name cannot be None"
                assert data["catalog_name"] is not None, "Catalog name cannot be None"
                return SchemaEntity(
                    namespace=namespace,
                    package=package,
                    typeName=type_name,
                    name=data["schema_name"],
                    URI=f"/{connector_name}/{connector_type}/{data['catalog_name']}/{data['schema_name']}",
                )
            except AssertionError as e:
                logger.error(f"Error creating SchemaEntity: {str(e)}")
                return None

        elif type_name == "TABLE":
            try:
                assert data["table_name"] is not None, "Table name cannot be None"
                assert data["table_catalog"] is not None, "Table catalog cannot be None"
                assert data["table_schema"] is not None, "Table schema cannot be None"

                if data.get("table_type") == "VIEW":
                    return ViewEntity(
                        namespace=namespace,
                        package=package,
                        typeName=type_name,
                        name=data["table_name"],
                        URI=f"/{connector_name}/{connector_type}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}",
                    )
                else:
                    return TableEntity(
                        namespace=namespace,
                        package=package,
                        typeName=type_name,
                        name=data["table_name"],
                        URI=f"/{connector_name}/{connector_type}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}",
                        isPartition=data.get("is_partition") or False,
                    )
            except AssertionError as e:
                logger.error(f"Error creating TableEntity: {str(e)}")
                return None

        elif type_name == "COLUMN":
            try:
                assert data["column_name"] is not None, "Column name cannot be None"
                assert data["table_catalog"] is not None, "Table catalog cannot be None"
                assert data["table_schema"] is not None, "Table schema cannot be None"
                assert data["table_name"] is not None, "Table name cannot be None"
                assert (
                    data["ordinal_position"] is not None
                ), "Ordinal position cannot be None"
                assert data["data_type"] is not None, "Data type cannot be None"

                return ColumnEntity(
                    namespace=namespace,
                    package=package,
                    typeName=type_name,
                    name=data["column_name"],
                    URI=f"/{connector_name}/{connector_type}/{data['table_catalog']}/{data['table_schema']}/{data['table_name']}/{data['column_name']}",
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
                typeName=type_name,
                name=name,
                URI=f"/{connector_name}/{connector_type}/{name}",
            )
