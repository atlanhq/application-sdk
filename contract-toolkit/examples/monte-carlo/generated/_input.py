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

    credential_guid: str = ""
    connection: ConnectionRef | None = None
    include_filter: Annotated[dict[str, Any], MaxItems(1000)] = Field(
        default_factory=dict
    )
    exclude_filter: Annotated[dict[str, Any], MaxItems(1000)] = Field(
        default_factory=dict
    )
    include_incident_feedback_status: Annotated[dict[str, Any], MaxItems(1000)] = Field(
        default_factory=dict
    )
    incident_days_filter: str = "30"
    """Select how far back you want to sync Incidents and Alerts from Monte Carlo"""
    advanced_config_strategy: str = "default"
    """Include specific SQL connections you want to enrich with monte carlo metadata"""
    asset_connection_qualified_names: str = ""
    """Atlan will only enrich Monte Carlo metadata for the selected connections, this is helpful when you have similar assets in dev or prod connection and only want to enrich one of those connection."""
    preflight_check: str = ""
    monte_carlo_credential: CredentialRef | None = None
    output_dir: str = ""
    """Directory for output JSONL files."""
    checkpoint_dir: str = ""
    """Directory for checkpoint database. If provided, enables incremental extraction."""
    load_to_atlan: bool = True
    """If True, load extracted metadata to Atlan via publish-app."""
    publish_dry_run: bool = False
    """When True, skip the Atlas publish step (executor_enabled=False)."""
