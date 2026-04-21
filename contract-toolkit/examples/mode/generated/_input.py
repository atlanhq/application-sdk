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
    credential_guid: str = ""
    connection: ConnectionRef | None = None
    exclude_collections: Annotated[dict[str, Any], MaxItems(1000)] = Field(
        default_factory=dict
    )
    """Assets from the selected collections will not be fetched."""
    include_collections: Annotated[dict[str, Any], MaxItems(1000)] = Field(
        default_factory=dict
    )
    """Assets from the selected collections will be fetched. Exclude gets preference over include."""
    crawl_archived_reports: bool = True
    """By default, the crawler retrieves all archived reports from Mode Analytics. Selecting 'No' will exclude them."""
    preflight_check: str = ""
    mode_credential: CredentialRef | None = None
    output_dir: str = ""
    """Directory for output JSONL files."""
    checkpoint_dir: str = ""
    """Directory for checkpoint database. If provided, enables incremental extraction."""
    load_to_atlan: bool = True
    """If True, load extracted metadata to Atlan via publish-app."""
    publish_dry_run: bool = False
    """When True, skip the Atlas publish step (executor_enabled=False)."""
