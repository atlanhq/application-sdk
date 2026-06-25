# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from application_sdk.testing.e2e.substitutions import SQLMustacheSubstitutions


class FullFeaturedMustacheSubstitutions(SQLMustacheSubstitutions):
    output_format: Literal["parquet", "json"] = Field(
        default="parquet",
        alias="{{output_format}}",
    )
    max_results: int = Field(default=500, alias="{{max_results}}")
    enable_lineage: bool = Field(default=False, alias="{{enable_lineage}}")
    load_to_atlan: bool = Field(default=True, alias="{{load-to-atlan}}")
    log_level: Literal["INFO", "DEBUG", "WARNING"] = Field(
        default="INFO",
        alias="{{log_level}}",
    )
    lineage_depth: int = Field(default=3, alias="{{lineage_depth}}")
    table_prefix: str = Field(default="", alias="{{table_prefix}}")
    custom_attributes: str = Field(default="", alias="{{custom_attributes}}")
    legacy_timeout: int = Field(default=60, alias="{{legacy_timeout}}")
    old_batch_mode: bool = Field(default=False, alias="{{old_batch_mode}}")
    schemas: dict[str, Any] | None = Field(default=None, alias="{{schemas}}")
