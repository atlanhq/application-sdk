"""Atlas transformer module for metadata transformation.

This module provides the Atlas transformer implementation for converting metadata
into Atlas entities using the pyatlan library.
"""

from datetime import datetime
from typing import Any, Dict, Optional, Type

from pyatlan.model.enums import AtlanConnectorType, EntityStatus

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.common.utils import process_text

logger = get_logger(__name__)


class AtlasTransformer(TransformerInterface):
    """Transformer for converting metadata into Atlas entities.

    This class implements the transformation of metadata into Atlas entities using
    the pyatlan library. It supports various entity types like databases, schemas,
    tables, columns, functions, and tag attachments.

    Attributes:
        current_epoch (str): Current epoch timestamp for versioning.
        connector_name (str): Name of the connector.
        tenant_id (str): ID of the tenant.
        entity_class_definitions (Dict[str, Type[Any]]): Mapping of entity types
            to their corresponding classes.
        connection_qualified_name (str): Qualified name for the connection.

    Example:
        >>> transformer = AtlasTransformer("sql-connector", "tenant123")
        >>> result = transformer.transform_metadata("DATABASE", data, "workflow1", "run1")
    """

    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        """Initialize the Atlas transformer.

        Args:
            connector_name (str): Name of the connector.
            tenant_id (str): ID of the tenant.
            **kwargs: Additional keyword arguments.
                current_epoch (str): Current epoch timestamp.
                connection_qualified_name (str): Qualified name for the connection.
        """
        from application_sdk.transformers.atlas.sql import (
            Column,
            Database,
            Function,
            Procedure,
            Schema,
            Table,
            TagAttachment,
        )

        self.current_epoch = kwargs.get("current_epoch", "0")
        self.connector_name = connector_name
        self.tenant_id = tenant_id
        self.entity_class_definitions: Dict[str, Type[Any]] = {
            "DATABASE": Database,
            "SCHEMA": Schema,
            "TABLE": Table,
            "VIEW": Table,
            "COLUMN": Column,
            "MATERIALIZED VIEW": Table,
            "FUNCTION": Function,
            "TAG_REF": TagAttachment,
            "PROCEDURE": Procedure,
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
        """Transform metadata into an Atlas entity.

        This method transforms the provided metadata into an Atlas entity based on
        the specified type. It also enriches the entity with workflow metadata.

        Args:
            typename (str): Type of the entity to create.
            data (Dict[str, Any]): Metadata to transform.
            workflow_id (str): ID of the workflow.
            workflow_run_id (str): ID of the workflow run.
            entity_class_definitions (Dict[str, Type[Any]], optional): Custom entity
                class definitions. Defaults to None.
            **kwargs: Additional keyword arguments.

        Returns:
            Optional[Dict[str, Any]]: The transformed entity as a dictionary, or None
                if transformation fails.

        Raises:
            Exception: If there's an error during entity deserialization.
        """
        typename = typename.upper()
        self.entity_class_definitions = (
            entity_class_definitions or self.entity_class_definitions
        )

        connection_qualified_name = kwargs.get("connection_qualified_name", None)
        connection_name = kwargs.get("connection_name", None)

        data.update(
            {
                "connection_qualified_name": connection_qualified_name,
                "connection_name": connection_name,
            }
        )

        creator = self.entity_class_definitions.get(typename)
        if creator:
            try:
                entity_attributes = creator.get_attributes(data)
                # enrich the entity with workflow metadata
                enriched_data = self._enrich_entity_with_metadata(
                    workflow_id, workflow_run_id, data
                )

                entity_attributes["attributes"].update(enriched_data["attributes"])
                entity_attributes["custom_attributes"].update(
                    enriched_data["custom_attributes"]
                )

                entity = entity_attributes["entity_class"](
                    attributes=entity_attributes["attributes"],
                    custom_attributes=entity_attributes["custom_attributes"],
                    status=EntityStatus.ACTIVE,
                )

                return entity.dict(by_alias=True, exclude_none=True, exclude_unset=True)
            except Exception as e:
                logger.error(
                    "Error transforming {} entity: {}",
                    typename,
                    str(e),
                    extra={"data": data},
                )
                return None
        else:
            logger.error(f"Unknown typename: {typename}")
            return None

    def _enrich_entity_with_metadata(
        self,
        workflow_id: str,
        workflow_run_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Enrich an entity with additional metadata.

        This method adds workflow metadata and other attributes to the entity.

        Args:
            entity (Asset): The entity to enrich.
            workflow_id (str): ID of the workflow.
            workflow_run_id (str): ID of the workflow run.
            data (Dict[str, Any]): Additional data for enrichment.

        Returns:
            Any: The enriched entity.
        """

        attributes = {}
        custom_attributes = {}

        attributes["status"] = EntityStatus.ACTIVE
        attributes["tenant_id"] = self.tenant_id
        attributes["last_sync_workflow_name"] = workflow_id
        attributes["last_sync_run"] = workflow_run_id
        attributes["last_sync_run_at"] = datetime.now()
        attributes["connection_name"] = data.get("connection_name", "")
        attributes["connector_name"] = AtlanConnectorType.get_connector_name(
            data.get("connection_qualified_name", "")
        )

        if remarks := data.get("remarks", None) or data.get("comment", None):
            attributes["description"] = process_text(remarks)

        if source_created_by := data.get("source_owner", None):
            attributes["source_created_by"] = source_created_by

        if created := data.get("created"):
            attributes["source_created_at"] = datetime.fromtimestamp(created / 1000)

        if last_altered := data.get("last_altered", None):
            attributes["source_updated_at"] = datetime.fromtimestamp(
                last_altered / 1000
            )

        if source_id := data.get("source_id", None):
            custom_attributes["source_id"] = source_id

        return {
            "attributes": attributes,
            "custom_attributes": custom_attributes,
        }
