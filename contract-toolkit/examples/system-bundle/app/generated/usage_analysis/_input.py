# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations

from application_sdk.contracts.base import Input
from pydantic import Field


class UsageAnalysisAppInputContract(Input):
    connection_qualified_name: str
    """Qualified name of the connection to analyse."""
    window_days: int = Field(default=30, ge=1)
    """Look-back window, in days."""
    dry_run: bool = False


AppInputContract = UsageAnalysisAppInputContract
