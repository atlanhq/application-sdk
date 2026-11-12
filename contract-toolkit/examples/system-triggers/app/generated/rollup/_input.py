# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations

from application_sdk.contracts.base import Input


class RollupAppInputContract(Input):
    window_days: int = 1


AppInputContract = RollupAppInputContract
