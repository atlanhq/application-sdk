"""Contracts for workflow configuration resolution.

The resolve_config task reads workflow configuration that was pre-pushed by
external orchestrators (e.g. Argo marketplace-scripts via
``POST /workflows/v1/config/{workflow_id}``) before the Temporal workflow
was started.

This replaces v2's ``get_workflow_args`` activity, which read the same
configuration from the Dapr state store.
"""

from __future__ import annotations

from typing import Any

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import ConnectionRef
from application_sdk.credentials.ref import CredentialRef


class ResolveConfigInput(Input):
    """Input for the resolve_config task."""

    workflow_id: str = ""
    """Temporal workflow ID — used as the config key."""


class ResolveConfigOutput(Output, allow_unbounded_fields=True):
    """Output from the resolve_config task.

    Contains the full workflow configuration retrieved from the state store.
    Fields that are not present in the stored config default to empty values.
    """

    credential_guid: str = ""
    """GUID of credentials stored in the secret store."""

    credential_ref: CredentialRef | None = None
    """Typed credential reference — preferred over credential_guid."""

    connection: ConnectionRef | None = None
    """Typed connection reference."""

    metadata: dict[str, Any] = {}
    """Workflow metadata (filters, configuration, etc.)."""

    output_path: str = ""
    """Local or object store path for output files."""

    output_prefix: str = ""
    """Object store prefix for all output artifacts."""

    exclude_filter: str = ""
    """Regex filter for excluding schemas/tables."""

    include_filter: str = ""
    """Regex filter for including schemas/tables."""

    temp_table_regex: str = ""
    """Regex pattern identifying temporary tables."""

    source_tag_prefix: str = ""
    """Tag prefix for source-level metadata."""
