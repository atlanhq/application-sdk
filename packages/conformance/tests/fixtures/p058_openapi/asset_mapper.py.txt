"""Asset mapping utilities for the OpenAPI Connector.

Maps typed OpenAPI extracted records to pyatlan Asset models and generates
qualified names following Atlan conventions.

Qualified Name Formats (from SPEC.md §4):
    Connection: provided as connection.qualified_name  (e.g. default/api/1234567)
    APISpec:    {connection_qn}/{spec_title}
    APIPath:    {spec_qn}{path_url}   (path_url starts with '/', so concat directly)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.api_types import OpenAPIPathRecord, OpenAPISpecRecord

from application_sdk.contracts.types import ConnectionRef

from pyatlan_v9.model.assets import APIPath, APISpec, Connection


def apply_sync_metadata(
    asset: "APIPath | APISpec",
    workflow_id: str,
    workflow_type: str,
    workflow_run_at_ms: int,
) -> None:
    """Stamp sync metadata fields onto a pyatlan Asset instance."""
    asset.status = "ACTIVE"
    asset.tenant_id = "default"
    asset.last_sync_run = workflow_id
    asset.last_sync_run_at = workflow_run_at_ms
    asset.last_sync_workflow_name = workflow_type


# Connector name constant — matches connector_name in the Connection asset
CONNECTOR_NAME = "api"


# =============================================================================
# Qualified Name Builders
# =============================================================================


def build_api_spec_qn(connection_qn: str, spec_title: str) -> str:
    """Build qualified name for an APISpec asset.

    Delegates to the pyatlan ``APISpec.creator()`` factory so the
    qualifiedName grammar lives in one place instead of being duplicated
    here as a format string.

    Args:
        connection_qn: Atlan connection qualified name.
        spec_title: The spec title from spec['info']['title'].
    """
    qualified_name = APISpec.creator(
        name=spec_title, connection_qualified_name=connection_qn
    ).qualified_name
    assert isinstance(qualified_name, str)
    return qualified_name


def build_api_path_qn(spec_qn: str, path_url: str) -> str:
    """Build qualified name for an APIPath asset.

    Delegates to the pyatlan ``APIPath.creator()`` factory so the
    qualifiedName grammar lives in one place instead of being duplicated
    here as a format string.

    Args:
        spec_qn: Parent APISpec qualified name.
        path_url: The path URL key from spec['paths'], e.g. '/pet/{petId}'.
    """
    qualified_name = APIPath.creator(
        path_raw_uri=path_url, spec_qualified_name=spec_qn
    ).qualified_name
    assert isinstance(qualified_name, str)
    return qualified_name


# =============================================================================
# Asset Mappers
# =============================================================================


def map_connection(connection: ConnectionRef) -> Connection:
    """Re-emit the Connection as an upsertable entity for the Atlan Loader.

    Args:
        connection: The ConnectionRef provided by the caller (from input.connection).
            Converted to a pyatlan_v9 Connection via ConnectionRef.to_connection(),
            then enriched with connector-specific fields.
    """
    conn_qn: str = connection.attributes.qualified_name
    _name = connection.attributes.name
    conn_name: str = _name if _name else conn_qn.rsplit("/", 1)[-1]
    native: Connection = connection.to_connection()
    native.connector_name = connection.attributes.connector_name or CONNECTOR_NAME
    native.connection_qualified_name = conn_qn
    native.name = conn_name
    native.category = connection.attributes.category or "API"
    return native


def map_api_spec(
    record: "OpenAPISpecRecord",
    connection_qn: str,
    workflow_id: str,
    workflow_type: str,
    workflow_run_at_ms: int,
) -> APISpec:
    """Create an APISpec Atlan asset from an OpenAPISpecRecord.

    Args:
        record: Extracted spec metadata from the OpenAPI info block.
        connection_qn: Atlan connection qualified name (base for QN).
        workflow_id: Temporal workflow ID.
        workflow_type: Temporal workflow type / app name.
        workflow_run_at_ms: Workflow start time as millisecond UNIX timestamp.
    """
    asset = APISpec.creator(name=record.title, connection_qualified_name=connection_qn)

    if record.spec_url:
        asset.source_url = record.spec_url
    if record.openapi_version:
        asset.api_spec_type = record.openapi_version
    if record.description:
        asset.description = record.description
    if record.terms_of_service:
        asset.api_spec_terms_of_service_url = record.terms_of_service
    if record.spec_version:
        asset.api_spec_version = record.spec_version

    contact = record.contact_email or record.contact_name or record.contact_url
    if contact:
        if record.contact_email:
            asset.api_spec_contact_email = record.contact_email
        if record.contact_name:
            asset.api_spec_contact_name = record.contact_name
        if record.contact_url:
            asset.api_spec_contact_url = record.contact_url

    license_info = record.license_name or record.license_url
    if license_info:
        if record.license_name:
            asset.api_spec_license_name = record.license_name
        if record.license_url:
            asset.api_spec_license_url = record.license_url

    external_docs = record.external_docs_url or record.external_docs_description
    if external_docs:
        docs: dict[str, str] = {}
        if record.external_docs_url:
            docs["url"] = record.external_docs_url
        if record.external_docs_description:
            docs["description"] = record.external_docs_description
        asset.api_external_docs = docs

    apply_sync_metadata(asset, workflow_id, workflow_type, workflow_run_at_ms)
    return asset


def map_api_path(
    record: "OpenAPIPathRecord",
    connection_qn: str,
    workflow_id: str,
    workflow_type: str,
    workflow_run_at_ms: int,
) -> APIPath:
    """Create an APIPath Atlan asset from an OpenAPIPathRecord.

    Args:
        record: Extracted path metadata from spec['paths'].
        connection_qn: Atlan connection qualified name.
        workflow_id: Temporal workflow ID.
        workflow_type: Temporal workflow type / app name.
        workflow_run_at_ms: Workflow start time as millisecond UNIX timestamp.
    """
    asset = APIPath.creator(
        path_raw_uri=record.path_url,
        spec_qualified_name=record.spec_qualified_name,
        connection_qualified_name=connection_qn,
    )
    asset.api_spec_name = record.spec_title
    asset.api_path_is_templated = record.is_templated

    if record.summary:
        asset.api_path_summary = record.summary
    if record.available_operations:
        asset.api_path_available_operations = record.available_operations
    if record.description:
        asset.description = record.description

    apply_sync_metadata(asset, workflow_id, workflow_type, workflow_run_at_ms)
    return asset
