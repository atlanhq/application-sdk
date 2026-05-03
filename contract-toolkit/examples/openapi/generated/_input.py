# AUTO-GENERATED from app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: make generate
from __future__ import annotations
from typing import Annotated, Any, ClassVar
from pydantic import Field
from application_sdk.contracts.types import ConnectionRef, FileReference, MaxItems
from application_sdk.credentials.ref import CredentialRef
from application_sdk.templates.contracts import ExtractionInput


class AppInputContract(ExtractionInput):
    _config_hash_exclude: ClassVar[set[str]] = {
        "output_dir",
        "checkpoint_dir",
        "load_to_atlan",
        "publish_dry_run",
    }

    import_type: str = "URL"
    """How to provide the spec: URL fetch or cloud object storage."""
    spec_url: str = ""
    """URL to the OpenAPI spec JSON/YAML document. Required when import type is URL."""
    spec_prefix: str = ""
    """Object store directory path. Required when import type is Cloud."""
    spec_key: str = ""
    """Object key or filename in the object store. Required when import type is Cloud."""
    cloud_source: str = ""
    """Cloud storage credential. Required when import type is Cloud."""
    output_dir: str = ""
    """Directory for output JSONL files."""
    checkpoint_dir: str = ""
    """Directory for checkpoint database. If provided, enables incremental extraction."""
    load_to_atlan: bool = True
    """If True, load extracted metadata to Atlan via publish-app."""
    publish_dry_run: bool = False
    """When True, skip the Atlas publish step (executor_enabled=False)."""
