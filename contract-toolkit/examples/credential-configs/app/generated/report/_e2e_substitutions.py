# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from __future__ import annotations

from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from pydantic import Field


class ReportMustacheSubstitutions(MustacheSubstitutions):
    objectstore_credential_guid: str = Field(
        default="",
        alias="{{objectstore-credential-guid}}",
    )
