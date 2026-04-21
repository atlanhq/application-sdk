# AUTO-GENERATED from app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: make generate
from __future__ import annotations
from typing import Annotated, Any, ClassVar
from pydantic import Field
from application_sdk.contracts.base import Input
from application_sdk.contracts.types import ConnectionRef, FileReference, MaxItems
from application_sdk.credentials.ref import CredentialRef


class AppInputContract(Input):
    _config_hash_exclude: ClassVar[set[str]] = {
        "output_dir",
        "checkpoint_dir",
        "load_to_atlan",
        "publish_dry_run",
    }

    database_api: str = "mongo"
    """Indicates API configured for your Azure Cosmos DB"""
    extraction_method: str = "direct"
    """Determines the method the package will use to extract metadata. 'Direct' means package will directly connect to the database."""
    credential_guid: str = ""
    evaluated_auth_type: str = "vcore"
    connection: ConnectionRef | None = None
    enable_schema_extraction: bool = False
    """Allow Atlan to analyze documents and extract collection schemas"""
    schema_extraction_sample_size: int = 100
    """Specify the number of documents for schema extraction analysis."""
    preflight_check: str = ""
    cosmos_credential: CredentialRef | None = None
    output_dir: str = ""
    """Directory for output JSONL files."""
    checkpoint_dir: str = ""
    """Directory for checkpoint database. If provided, enables incremental extraction."""
    load_to_atlan: bool = True
    """If True, load extracted metadata to Atlan via publish-app."""
    publish_dry_run: bool = False
    """When True, skip the Atlas publish step (executor_enabled=False)."""
