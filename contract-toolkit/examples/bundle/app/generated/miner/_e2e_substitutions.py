# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from __future__ import annotations

from pydantic import Field

from application_sdk.testing.e2e.substitutions import MustacheSubstitutions


class MinerMustacheSubstitutions(MustacheSubstitutions):
    lookback_days: int = Field(default=30, alias="{{lookback-days}}")
    max_queries: int = Field(default=0, alias="{{max-queries}}")
