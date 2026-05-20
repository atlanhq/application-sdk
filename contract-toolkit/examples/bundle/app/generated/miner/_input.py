# AUTO-GENERATED from app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: pkl eval -m . contract/app.pkl
from __future__ import annotations
from typing import Annotated, Any, ClassVar
from pydantic import Field
from application_sdk.contracts.types import ConnectionRef, FileReference, MaxItems
from application_sdk.credentials.ref import CredentialRef
from application_sdk.templates.contracts import ExtractionInput


class AppInputContract(ExtractionInput):
    lookback_days: int = 30
    """Number of days of query history to mine."""
    max_queries: int = 0
    """Cap on the number of queries to mine per run. 0 = unlimited."""
