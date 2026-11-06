# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations

from application_sdk.templates.contracts import ExtractionInput


class AppInputContract(ExtractionInput):
    qn_prefix: str = ""
    """Export assets under this prefix."""
