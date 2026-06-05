# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from __future__ import annotations

from pydantic import Field

from application_sdk.testing.e2e.substitutions import SQLMustacheSubstitutions


class PublishControlsMustacheSubstitutions(SQLMustacheSubstitutions):
    load_to_atlan: bool = Field(default=True, alias="{{load-to-atlan}}")
