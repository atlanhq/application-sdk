import logging
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel

from application_sdk.workflows.transformers import TransformerInterface
from application_sdk.workflows.transformers.phoenix.schema import (
    BaseObjectEntity,
    ColumnConstraint,
    ColumnEntity,
    DatabaseEntity,
    FunctionEntity,
    Namespace,
    Package,
    SchemaEntity,
    SnowflakePipeEntity,
    StageEntity,
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

    def transform_metadata(
        self, typename: str, data: Dict[str, Any], **kwargs: Any
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
        transform_method: Dict[str, Callable[[Dict[str, Any]], Optional[BaseModel]]] = {
            "DATABASE": self._transform_database,
            "SCHEMA": self._transform_schema,
            "TABLE": self._transform_table,
            "COLUMN": self._transform_column,
            "STAGE": self._transform_stage,
            "PIPE": self._transform_pipe,
            "FUNCTION": self._transform_function,
        }

        if transform_method:
            entity = transform_method[type_name](data)
            if entity:
                return entity.model_dump()
            else:
                return None
        else:
            logger.error(f"Unknown typename: {typename}")
            return self._transform_default(type_name, data).model_dump()

    def _transform_database(self, data: Dict[str, Any]) -> Optional[DatabaseEntity]:
        try:
            self._assert_not_none(data, "datname", "Database name")
            return DatabaseEntity(
                namespace=self.namespace,
                package=self.package,
                typeName="DATABASE",
                name=data["datname"],
                URI=self._build_uri(data["datname"]),
            )
        except AssertionError as e:
            logger.error(f"Error creating DatabaseEntity: {str(e)}")
            return None

    def _transform_schema(self, data: Dict[str, Any]) -> Optional[SchemaEntity]:
        try:
            self._assert_not_none(data, "schema_name", "Schema name")
            self._assert_not_none(data, "catalog_name", "Catalog name")
            return SchemaEntity(
                namespace=self.namespace,
                package=self.package,
                typeName="SCHEMA",
                name=data["schema_name"],
                URI=self._build_uri(data["catalog_name"], data["schema_name"]),
            )
        except AssertionError as e:
            logger.error(f"Error creating SchemaEntity: {str(e)}")
            return None

    def _transform_table(self, data: Dict[str, Any]) -> Optional[BaseObjectEntity]:
        try:
            self._assert_not_none(data, "table_name", "Table name")
            self._assert_not_none(data, "table_catalog", "Table catalog")
            self._assert_not_none(data, "table_schema", "Table schema")

            if data.get("table_type") == "TABLE":
                return TableEntity(
                    namespace=self.namespace,
                    package=self.package,
                    typeName="TABLE",
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
                    typeName="VIEW",
                    name=data["table_name"],
                    URI=self._build_uri(
                        data["table_catalog"], data["table_schema"], data["table_name"]
                    ),
                )
        except AssertionError as e:
            logger.error(f"Error creating TableEntity: {str(e)}")
            return None

    def _transform_column(self, data: Dict[str, Any]) -> Optional[ColumnEntity]:
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
                typeName="COLUMN",
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

    def _transform_stage(self, data: Dict[str, Any]) -> Optional[StageEntity]:
        try:
            self._assert_not_none(data, "stage_id", "Stage ID")
            self._assert_not_none(data, "stage_name", "Stage Name")
            self._assert_not_none(data, "stage_schema", "Stage Schema")

            return StageEntity(
                namespace=self.namespace,
                package=self.package,
                typeName="STAGE",
                id=data["stage_id"],
                name=data["stage_name"],
                schema=data["stage_schema"],
                URI=self._build_uri(
                    data["stage_catalog"],
                    data["stage_schema"],
                    data["stage_name"],
                ),
            )
        except AssertionError as e:
            logger.error(f"Error creating StageEntity: {str(e)}")
            return None

    def _transform_pipe(self, data: Dict[str, Any]) -> Optional[SnowflakePipeEntity]:
        try:
            self._assert_not_none(data, "pipe_id", "Pipe ID")
            self._assert_not_none(data, "pipe_name", "Pipe Name")
            self._assert_not_none(data, "pipe_schema", "Pipe Schema")

            return SnowflakePipeEntity(
                namespace=self.namespace,
                package=self.package,
                typeName="PIPE",
                id=data["pipe_id"],
                name=data["pipe_name"],
                schema=data["pipe_schema"],
                URI=self._build_uri(
                    data["pipe_catalog"],
                    data["pipe_schema"],
                    data["pipe_name"],
                ),
                snowflakePipeIsAutoIngestEnabled=data["is_autoingest_enabled"],
                snowflakePipeNotificationChannelName=data["notification_channel_name"],
            )
        except AssertionError as e:
            logger.error(f"Error creating SnowflakePipeEntity: {str(e)}")
            return None

    def _transform_function(self, data: Dict[str, Any]) -> Optional[FunctionEntity]:
        try:
            self._assert_not_none(data, "function_id", "Function ID")
            self._assert_not_none(data, "function_name", "Function name")
            self._assert_not_none(data, "function_schema", "Function schema")

            return FunctionEntity(
                namespace=self.namespace,
                package=self.package,
                typeName="FUNCTION",
                id=data["function_id"],
                name=data["function_name"],
                schema=data["function_schema"],
                URI=self._build_uri(
                    data["function_catalog"],
                    data["function_schema"],
                    data["function_name"],
                ),
                functionArguments=data["argument_signature"],
                functionDefinition=data["function_definition"],
                functionIsExternal=data["is_external"] == "YES",
                functionIsMemoizable=data["is_memoizable"] == "YES",
                functionLanguage=data["function_language"],
                functionReturnType=data["data_type"],
                functionIsSecure=True,
                functionType="",
            )
        except AssertionError as e:
            logger.error(f"Error creating FunctionEntity: {str(e)}")
            return None

    def _transform_default(
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
