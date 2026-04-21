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

    connection_qualified_name: str = ""
    extraction_method: str = "query_history"
    """Determines the method to use to fetch the raw metadata for mining"""
    agent_json: Annotated[dict[str, Any], MaxItems(1000)] = Field(default_factory=dict)
    miner_start_time_epoch: str = ""
    """Start time epoch for miner query extraction."""
    advanced_config: str = "default"
    """Set advanced configuration of the miner"""
    cross_connection: str = "false"
    """Enable searching for lineage across all available connections."""
    control_config_strategy: str = "default"
    """Controls custom experimental feature flags for the miner"""
    control_config: str = ""
    """Custom JSON config controlling experimental feature flags."""
    preflight_check: str = ""
    teradata_miner_credential: CredentialRef | None = None
    output_dir: str = ""
    """Directory for output JSONL files."""
    checkpoint_dir: str = ""
    """Directory for checkpoint database. If provided, enables incremental extraction."""
    load_to_atlan: bool = True
    """If True, load extracted metadata to Atlan via publish-app."""
    publish_dry_run: bool = False
    """When True, skip the Atlas publish step (executor_enabled=False)."""
