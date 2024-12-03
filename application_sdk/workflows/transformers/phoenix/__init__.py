import logging
from typing import Any, Callable, Dict, Optional

from application_sdk.workflows.transformers import TransformerInterface
from application_sdk.workflows.transformers.const import (
    COLUMN,
    DATABASE,
    SCHEMA,
    TABLE,
    VIEW,
)
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
    It uses the schema module to create the entities.

    Usage:
        Subclass this class and override the transform_metadata method to customize the transformation process.
        Then use the subclass as an argument to the SQLWorkflowWorker.

        >>> class CustomPhoenixTransformer(PhoenixTransformer):
        >>>     def transform_metadata(self, typename: str, data: Dict[str, Any], **kwargs: Any) -> Optional[str]:
        >>>         # Custom logic here
    """

    def __init__(self, connector_name: str, **kwargs: Any):
        self.connector_name = connector_name
        self.connector_type = kwargs.get("connector_type", "phoenix")
        self.connector_temp = f"{self.connector_name}-{self.connector_type}"
        self.namespace = Namespace(id=self.connector_temp, name=self.connector_temp)
        self.package = Package(id=self.connector_temp, name=self.connector_temp)

        self.entity_creator_map: Dict[
            str, Callable[[Dict[str, Any]], Optional[Any]]
        ] = {
            DATABASE: self._create_database_entity,
            SCHEMA: self._create_schema_entity,
            TABLE: self._create_table_entity,
            COLUMN: self._create_column_entity,
        }

    def transform_metadata(
        self,
        typename: str,
        data: Dict[str, Any],
        entity_creator_map: Dict[str, Callable[[Dict[str, Any]], Optional[Any]]]
        | None = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Transform metadata to Atlan Open Spec.

        Args:
            typename (str): The type of the metadata.
            data (Dict[str, Any]): The metadata.
            **kwargs (Any): Additional arguments.

        Returns:
            Optional[str]: The json string of the transformed metadata.
        """
        type_name = typename.upper()
        self.entity_creator_map = entity_creator_map or self.entity_creator_map

        creator = self.entity_creator_map.get(type_name)
        if creator:
            entity = creator(data)
            if entity:
                return entity.model_dump()
            else:
                logger.error(f"Error deserializing {type_name} entity: {data}")
                return None
        else:
            logger.error(f"Unknown typename: {typename}, creating default entity")
            return self._create_default_entity(type_name, data).model_dump()

    def _create_database_entity(self, data: Dict[str, Any]) -> Optional[DatabaseEntity]:
        try:
            self._assert_not_none(data, "database_name", "Database name")
            return DatabaseEntity(
                namespace=self.namespace,
                package=self.package,
                typeName=DATABASE,
                name=data["database_name"],
                URI=self._build_uri(data["database_name"]),
            )
        except AssertionError as e:
            logger.error(f"Error creating DatabaseEntity: {str(e)}")
            return None

    def _create_schema_entity(self, data: Dict[str, Any]) -> Optional[SchemaEntity]:
        try:
            self._assert_not_none(data, "schema_name", "Schema name")
            self._assert_not_none(data, "catalog_name", "Catalog name")
            return SchemaEntity(
                namespace=self.namespace,
                package=self.package,
                typeName=SCHEMA,
                name=data["schema_name"],
                URI=self._build_uri(data["catalog_name"], data["schema_name"]),
            )
        except AssertionError as e:
            logger.error(f"Error creating SchemaEntity: {str(e)}")
            return None

    def _create_table_entity(self, data: Dict[str, Any]) -> Optional[TableEntity]:
        try:
            self._assert_not_none(data, "table_name", "Table name")
            self._assert_not_none(data, "table_catalog", "Table catalog")
            self._assert_not_none(data, "table_schema", "Table schema")

            if data.get("table_type") == TABLE:
                return TableEntity(
                    namespace=self.namespace,
                    package=self.package,
                    typeName=TABLE,
                    name=data["table_name"],
                    URI=self._build_uri(
                        data["table_catalog"], data["table_schema"], data["table_name"]
                    ),
                    isPartition=data.get("is_partition") or False,
                )
            else:
                return ViewEntity(
                    namespace=self.namespace,
                    package=self.package,
                    typeName=VIEW,
                    name=data["table_name"],
                    URI=self._build_uri(
                        data["table_catalog"], data["table_schema"], data["table_name"]
                    ),
                )
        except AssertionError as e:
            logger.error(f"Error creating TableEntity: {str(e)}")
            return None

    def _create_column_entity(self, data: Dict[str, Any]) -> Optional[ColumnEntity]:
        try:
            self._assert_not_none(data, "column_name", "Column name")
            self._assert_not_none(data, "table_catalog", "Table catalog")
            self._assert_not_none(data, "table_schema", "Table schema")
            self._assert_not_none(data, "table_name", "Table name")
            self._assert_not_none(data, "ordinal_position", "Ordinal position")
            self._assert_not_none(data, "data_type", "Data type")

            return ColumnEntity(
                namespace=self.namespace,
                package=self.package,
                typeName=COLUMN,
                name=data["column_name"],
                URI=self._build_uri(
                    data["table_catalog"],
                    data["table_schema"],
                    data["table_name"],
                    data["column_name"],
                ),
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

    def _create_default_entity(
        self, type_name: str, data: Dict[str, Any]
    ) -> BaseObjectEntity:
        name = data[f"{type_name.lower()}_name"]
        return BaseObjectEntity(
            typeName=type_name,
            name=name,
            URI=self._build_uri(name),
        )

    def _assert_not_none(self, data: Dict[str, Any], key: str, error_message: str):
        assert data.get(key) is not None, f"{error_message} cannot be None"

    def _build_uri(self, *args: str) -> str:
        return f"/{self.connector_name}/{self.connector_type}/{'/'.join(args)}"
