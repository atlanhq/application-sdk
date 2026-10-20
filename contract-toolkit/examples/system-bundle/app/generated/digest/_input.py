# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations

from application_sdk.contracts.base import Input


class DigestAppInputContract(Input):
    input_prefix: str
    """Object-store prefix the upstream node wrote its output to."""
    max_items: int = 100


AppInputContract = DigestAppInputContract
