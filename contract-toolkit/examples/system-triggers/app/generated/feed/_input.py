# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations

from application_sdk.contracts.base import Input


class FeedAppInputContract(Input):
    target: str = ""
    batch_key: str = ""
    """Object-store key holding this micro-batch's events; AE always sets it."""


AppInputContract = FeedAppInputContract
