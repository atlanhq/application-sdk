# AUTO-GENERATED from app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations
import json
from typing import Annotated, Any, ClassVar
from pydantic import Field, field_validator
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

    output_format: str = "parquet"
    """Format for extracted metadata files."""
    max_results: int = 500
    """Cap on assets returned per schema."""
    enable_lineage: bool = False
    """Parse query history for column-level lineage."""
    load_to_atlan: bool = True
    """Publish extracted assets; uncheck for a dry run."""
    log_level: str = "INFO"
    """Verbosity for the extraction run."""
    lineage_depth: int = 3
    """How many hops of upstream lineage to resolve (required when lineage is on)."""
    schemas: Annotated[dict[str, str], MaxItems(1000)] = Field(default_factory=dict)

    @field_validator("schemas", mode="before")
    @classmethod
    def _coerce_json_object_strings(cls, value: Any) -> Any:
        if value is None or isinstance(value, dict):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return {}
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            if isinstance(parsed, dict):
                return parsed
        return value

    full_featured_credential: CredentialRef | None = None
    output_dir: str = ""
    """Directory for output JSONL files."""
    checkpoint_dir: str = ""
    """Directory for checkpoint database. If provided, enables incremental extraction."""
    load_to_atlan: bool = True
    """If True, load extracted metadata to Atlan via publish-app."""
    publish_dry_run: bool = False
    """When True, skip the Atlas publish step (executor_enabled=False)."""
