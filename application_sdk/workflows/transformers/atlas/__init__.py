import logging
from typing import Any, Dict, Optional, Type

from application_sdk.workflows.transformers import TransformerInterface
from application_sdk.workflows.transformers.atlas.sql import (
    Column,
    Database,
    Schema,
    Table,
)

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
        >>>     def transform_metadata(self, typename: str, data: Dict[str, Any], **kwargs: Any) -> Optional[str]:
        >>>         # Custom logic here
    """

    def __init__(self, connector_name: str, **kwargs: Any):
        self.current_epoch = kwargs.get("current_epoch", "0")
        self.connector_name = connector_name
        self.entity_class_definitions: Dict[str, Type[Any]] = {
            "DATABASE": Database,
            "SCHEMA": Schema,
            "TABLE": Table,
            "VIEW": Table,
            "COLUMN": Column,
        }

        self.connection_qualified_name = kwargs.get(
            "connection_qualified_name",
            f"default/{self.connector_name}/{self.current_epoch}",
        )

    def transform_metadata(
        self,
        typename: str,
        data: Dict[str, Any],
        entity_class_definitions: Dict[str, Type[Any]] | None = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        typename = typename.upper()
        self.entity_class_definitions = (
            entity_class_definitions or self.entity_class_definitions
        )

        data.update({"connection_qualified_name": self.connection_qualified_name})

        creator = self.entity_class_definitions.get(typename)
        if creator:
            try:
                entity = creator.parse_obj(data)
                return entity.dict(by_alias=True, exclude_none=True)
            except Exception as e:
                logger.error(f"Error deserializing {typename} entity {data}: {e}")
                return None
        else:
            logger.error(f"Unknown typename: {typename}")
            return None
