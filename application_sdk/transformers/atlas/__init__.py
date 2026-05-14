"""Atlas transformer module for metadata transformation.

This module provides the Atlas transformer implementation for converting metadata
into Atlas entities using the pyatlan library.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pyatlan.model.enums import AtlanConnectorType, EntityStatus

if TYPE_CHECKING:
    import daft

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.common.last_sync import (
    LastSyncDetails,
    resolve_last_sync_details,
    set_last_sync_details_on_asset,
)
from application_sdk.transformers.common.utils import process_text

logger = get_logger(__name__)

# Module-level sentinel so the legacy-args fallback warning fires once per
# process (not once per asset).  In production this surfaces a missing
# Temporal interceptor or a non-Temporal entry path; in tests the legacy
# fixtures fire it once, which keeps the test log readable.
_LEGACY_LAST_SYNC_WARNED: bool = False


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
        from application_sdk.transformers.atlas.sql import (  # noqa: PLC0415 — circular: transformers/atlas/__init__.py loads sibling sql submodule
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
        self.entity_class_definitions: dict[str, type[Any]] = {
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
        dataframe: daft.DataFrame,
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: dict[str, type[Any]] | None = None,
        **kwargs: dict[str, Any],
    ) -> daft.DataFrame:
        self.entity_class_definitions = (
            entity_class_definitions or self.entity_class_definitions
        )
        connection_name = kwargs.get("connection", {}).get("connection_name", None)
        connection_qualified_name = kwargs.get("connection", {}).get(
            "connection_qualified_name", None
        )
        transformed_metadata_list = []
        for row in dataframe.iter_rows():
            try:
                transformed_metadata = self.transform_row(
                    typename,
                    row,
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run_id,
                    connection_name=connection_name,
                    connection_qualified_name=connection_qualified_name,
                )
                if transformed_metadata:
                    transformed_metadata_list.append(transformed_metadata)
                else:
                    logger.warning(
                        "Skipped invalid data: typename=%s keys=%s sha=%s",
                        typename,
                        sorted(row.keys()) if isinstance(row, dict) else None,
                        hashlib.sha256(
                            json.dumps(row, sort_keys=True, default=str).encode("utf-8")
                        ).hexdigest()[:16]
                        if isinstance(row, dict)
                        else None,
                    )
            except Exception:
                logger.error("Error processing row: %s", typename, exc_info=True)

        import daft  # noqa: PLC0415 — optional dep: daft

        return daft.from_pylist(transformed_metadata_list)

    def transform_row(
        self,
        typename: str,
        data: dict[str, Any],
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: dict[str, type[Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
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
                enriched_data = self._enrich_entity_with_metadata(data)

                entity_attributes["attributes"].update(enriched_data["attributes"])
                entity_attributes["custom_attributes"].update(
                    enriched_data["custom_attributes"]
                )

                entity = entity_attributes["entity_class"](
                    attributes=entity_attributes["attributes"],
                    custom_attributes=entity_attributes["custom_attributes"],
                    status=EntityStatus.ACTIVE,
                )

                # Stamp last-sync details on the typed pyatlan ``Asset``
                # *before* serialisation — the dict round-trip path is
                # intentionally not used so all transformations converge on
                # the single Asset-based primitive (BLDX-1229).  The legacy
                # ``workflow_id`` / ``workflow_run_id`` method args are
                # passed only as fallbacks: the resolver reads execution +
                # correlation context first (set by the Temporal
                # interceptor in production), and the explicit args are
                # used only when the context is empty (CLI tools, unit
                # tests without Temporal).
                resolved = resolve_last_sync_details()
                if not resolved.workflow_name and not resolved.run:
                    global _LEGACY_LAST_SYNC_WARNED
                    if not _LEGACY_LAST_SYNC_WARNED:
                        _LEGACY_LAST_SYNC_WARNED = True
                        logger.warning(
                            "AtlasTransformer falling back to legacy "
                            "workflow_id / workflow_run_id args for last-sync "
                            "enrichment because the SDK execution / correlation "
                            "context is empty. In a Temporal worker this "
                            "indicates the SDK's interceptors are not registered; "
                            "assets will carry the child workflow's Temporal id "
                            "instead of the AE workflow id (BLDX-1229)."
                        )
                set_last_sync_details_on_asset(
                    entity,
                    details=LastSyncDetails(
                        run=resolved.run or workflow_run_id,
                        workflow_name=resolved.workflow_name or workflow_id,
                        run_at_ms=resolved.run_at_ms,
                    ),
                )

                return entity.dict(by_alias=True, exclude_none=True, exclude_unset=True)
            except Exception:
                logger.error("Error transforming entity: %s", typename, exc_info=True)
                return None
        else:
            logger.error("Unknown typename: %s", typename)
            return None

    def _enrich_entity_with_metadata(
        self,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Enrich an entity with non-last-sync metadata from the source row.

        Returns the attributes that get merged into the pyatlan asset
        construction kwargs.  Last-sync stamping is intentionally *not*
        done here — it happens after entity construction via
        :func:`set_last_sync_details_on_asset` on the typed Asset, so the
        single Asset-based primitive is the only path that writes those
        fields (BLDX-1229).
        """

        attributes: dict[str, Any] = {}
        custom_attributes: dict[str, Any] = {}

        attributes["status"] = EntityStatus.ACTIVE
        attributes["tenant_id"] = self.tenant_id
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
