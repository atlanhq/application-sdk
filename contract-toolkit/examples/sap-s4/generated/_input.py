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

    cloud_provider: str = ""
    extraction_method: str = "direct"
    """Determines the method the package will use to extract metadata. 'Direct' means the package will directly connect to SAP. 'Agent' means metadata is fetched via the Atlan secure agent."""
    credential_guid: str = ""
    agent_json: Annotated[dict[str, Any], MaxItems(1000)] = Field(default_factory=dict)
    connection: ConnectionRef | None = None
    component_include_filter: Annotated[dict[str, Any], MaxItems(1000)] = Field(
        default_factory=dict
    )
    """Selected components will be processed."""
    component_exclude_filter: Annotated[dict[str, Any], MaxItems(1000)] = Field(
        default_factory=dict
    )
    """Selected components will be excluded."""
    connection_pool_size: int = 5
    """Max number of connections that can be created for a destination simultaneously"""
    asset_types: str = ""
    """Select asset types to crawl."""
    preflight_check: str = ""
    sap_s4_credential: CredentialRef | None = None
    output_dir: str = ""
    """Directory for output JSONL files."""
    checkpoint_dir: str = ""
    """Directory for checkpoint database. If provided, enables incremental extraction."""
    load_to_atlan: bool = True
    """If True, load extracted metadata to Atlan via publish-app."""
    publish_dry_run: bool = False
    """When True, skip the Atlas publish step (executor_enabled=False)."""
