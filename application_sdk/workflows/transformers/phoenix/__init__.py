import logging
from typing import Any, Dict, Optional, Type

from application_sdk.workflows.transformers import TransformerInterface
from application_sdk.workflows.transformers.phoenix.sql import (
    BaseObjectEntity,
    ColumnEntity,
    DatabaseEntity,
    Namespace,
    Package,
    SchemaEntity,
    TableEntity,
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

        self.entity_class_definitions: Dict[str, Type[Any]] = {
            "DATABASE": DatabaseEntity,
            "SCHEMA": SchemaEntity,
            "TABLE": TableEntity,
            "VIEW": TableEntity,
            "COLUMN": ColumnEntity,
        }

    def transform_metadata(
        self,
        typename: str,
        data: Dict[str, Any],
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: Dict[str, Type[Any]] | None = None,
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
        self.entity_class_definitions = (
            entity_class_definitions or self.entity_class_definitions
        )

        data.update(
            {
                "namespace": self.namespace,
                "package": self.package,
                "typeName": type_name,
                "connector_name": self.connector_name,
                "connector_type": self.connector_type,
                "workflow_id": workflow_id,
                "workflow_run_id": workflow_run_id,
            }
        )

        entity_class = self.entity_class_definitions.get(type_name)
        if entity_class:
            entity = entity_class.parse_obj(data)
            if entity:
                return entity.json(by_alias=True, exclude_none=True)
            else:
                logger.error(f"Error deserializing {type_name} entity: {data}")
                return None
        else:
            logger.error(f"Unknown typename: {typename}, creating default entity")
            return BaseObjectEntity.parse_obj(data).model_dump()
