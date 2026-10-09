# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from application_sdk.contracts.base import Input
from application_sdk.contracts.types import MaxItems


class ColumnMapping(BaseModel):
    """SQL expressions mapping source columns to the canonical schema."""

    model_config = ConfigDict(extra="forbid")

    query_id: str = "QUERY_ID"
    query_text: str = "QUERY_TEXT"
    native_payload: str | None = None
    """Optional per-row payload column; unset for sources without one."""
    case_insensitive_match: bool = True


class AppInputContract(Input):
    connection_qualified_name: str
    """Qualified name of the connection to process."""
    input_prefix: str
    """Object-store prefix the upstream node wrote its output to."""
    connection_name: str = ""
    source_tag: str | None = "atlan"
    """Tag stamped on processed assets; None stamps none."""
    window_days: int = Field(default=30, ge=1)
    """Look-back window, in days."""
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    lake_provider: Literal["local", "aws", "gcp", "azure"] = "local"
    column_mapping: ColumnMapping = Field(default_factory=ColumnMapping)
    """SQL expressions mapping source columns to the canonical schema."""
    include_filter: Annotated[
        dict[str, Annotated[list[str], MaxItems(1000)]], MaxItems(1000)
    ] = Field(default_factory=dict)
    """Database name to the schema names to include."""
    types_to_ignore: Annotated[list[str], MaxItems(1000)] = Field(
        default_factory=lambda: ["SHOW", "DESCRIBE"]
    )
    dry_run: bool = False
    legacy_mode: bool = Field(default=False, deprecated="Ignored; will be removed.")
