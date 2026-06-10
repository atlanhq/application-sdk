# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from application_sdk.contracts.types import ConnectionRef, MaxItems
from application_sdk.templates.contracts import ExtractionInput


class AppInputContract(ExtractionInput):
    connections: Annotated[list[ConnectionRef], MaxItems(1000)] = Field(
        default_factory=list
    )
