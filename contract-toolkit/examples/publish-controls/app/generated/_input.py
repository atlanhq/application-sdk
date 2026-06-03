# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations

from application_sdk.credentials.ref import CredentialRef
from application_sdk.templates.contracts import ExtractionInput


class AppInputContract(ExtractionInput):
    load_to_atlan: bool = True
    publish_controls_credential: CredentialRef | None = None
