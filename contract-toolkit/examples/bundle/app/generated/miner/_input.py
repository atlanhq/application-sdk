# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations
from application_sdk.templates.contracts import ExtractionInput


class AppInputContract(ExtractionInput):
    lookback_days: int = 30
    """Number of days of query history to mine."""
    max_queries: int = 0
    """Cap on the number of queries to mine per run. 0 = unlimited."""
