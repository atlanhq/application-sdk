# AUTO-GENERATED from app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: make generate
from __future__ import annotations
from typing import Annotated, Any, ClassVar
from pydantic import Field
from application_sdk.contracts.base import Input
from application_sdk.contracts.types import ConnectionRef, FileReference, MaxItems
from application_sdk.credentials.ref import CredentialRef


class AppInputContract(Input, allow_unbounded_fields=True):
    _config_hash_exclude: ClassVar[set[str]] = {
        "output_dir",
        "checkpoint_dir",
        "load_to_atlan",
        "publish_dry_run",
    }

    extraction_method: str = "direct"
    """Determines the method the package will use to extract metadata."""
    credential_guid: str = ""
    connection: ConnectionRef | None = None
    publish_mode: str = "production"
    include_filter: Annotated[dict[str, Any], MaxItems(1000)] = Field(
        default_factory=dict
    )
    exclude_filter: Annotated[dict[str, Any], MaxItems(1000)] = Field(
        default_factory=dict
    )
    preflight_check: str = ""
    advanced_config_strategy: str = "default"
    """Set advanced configuration of the crawler"""
    use_source_schema_filtering: str = "false"
    """Enable or Disable Schema Level Filtering on source."""
    use_jdbc_internal_methods: str = "true"
    """Enable or Disable JDBC internal methods for data extraction."""
    trino_credential: CredentialRef | None = None
    output_dir: str = ""
    """Directory for output JSONL files."""
    checkpoint_dir: str = ""
    """Directory for checkpoint database. If provided, enables incremental extraction."""
    load_to_atlan: bool = True
    """If True, load extracted metadata to Atlan via publish-app."""
    publish_dry_run: bool = False
    """When True, skip the Atlas publish step (executor_enabled=False)."""
