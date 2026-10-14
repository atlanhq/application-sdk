# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from __future__ import annotations

from typing import Any

from pydantic import Field

from application_sdk.testing.e2e.substitutions import MustacheSubstitutions


class TreeFiltersMustacheSubstitutions(MustacheSubstitutions):
    include_filter: dict[str, Any] | None = Field(
        default=None,
        alias="{{include-filter}}",
    )
    exclude_filter: dict[str, Any] | None = Field(
        default=None,
        alias="{{exclude-filter}}",
    )
