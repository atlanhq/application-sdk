import logging
from datetime import datetime
from typing import Any, Dict, Optional, Type

from pyatlan.model.assets import Asset

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.atlas.sql import (
    Column,
    Database,
    Function,
    Schema,
    Table,
    TagAttachment,
)
from application_sdk.transformers.common.utils import process_text

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


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

    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        self.connector_name = connector_name
        self.tenant_id = tenant_id
        self.entity_class_definitions: Dict[str, Type[Any]] = {
            "DATABASE": Database,
            "SCHEMA": Schema,
            "TABLE": Table,
            "VIEW": Table,
            "COLUMN": Column,
            "FUNCTION": Function,
            "TAG_REF": TagAttachment,
        }

    def transform_metadata(
        self,
        typename: str,
        data: Dict[str, Any],
        workflow_id: str,
        workflow_run_id: str,
        connection_name: str,
        connection_qualified_name: str,
        entity_class_definitions: Dict[str, Type[Any]] | None = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        typename = typename.upper()
        self.entity_class_definitions = (
            entity_class_definitions or self.entity_class_definitions
        )

        data.update(
            {
                "connection_qualified_name": connection_qualified_name,
                "connection_name": connection_name,
            }
        )

        creator = self.entity_class_definitions.get(typename)
        if creator:
            try:
                entity = creator.parse_obj(data)

                # enrich the entity with workflow metadata
                entity = self._enrich_entity_with_metadata(
                    entity, workflow_id, workflow_run_id, data
                )

                return entity.dict(by_alias=True, exclude_none=True)
            except Exception as e:
                logger.error(f"Error deserializing {typename} entity {data}: {e}")
                return None
        else:
            logger.error(f"Unknown typename: {typename}")
            return None

    def _enrich_entity_with_metadata(
        self,
        entity: Asset,
        workflow_id: str,
        workflow_run_id: str,
        data: Dict[str, Any],
    ) -> Any:
        entity.tenant_id = self.tenant_id
        entity.last_sync_workflow_name = workflow_id
        entity.last_sync_run = workflow_run_id
        entity.last_sync_run_at = datetime.now()
        entity.connection_name = data.get("connection_name", "")

        if remarks := data.get("remarks", None) or data.get("comment", None):
            entity.description = process_text(remarks)

        if source_created_by := data.get("source_owner", None):
            entity.source_created_by = source_created_by

        if created := data.get("created"):
            entity.source_created_at = datetime.fromtimestamp(created / 1000)

        if last_altered := data.get("last_altered", None):
            entity.source_updated_at = datetime.fromtimestamp(last_altered / 1000)

        if not entity.custom_attributes:
            entity.custom_attributes = {}

        if source_id := data.get("source_id", None):
            entity.custom_attributes["source_id"] = source_id

        return entity
