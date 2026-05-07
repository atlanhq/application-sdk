# AUTO-GENERATED from app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: make generate
from __future__ import annotations
from typing import Annotated, Any, ClassVar
from pydantic import Field
from application_sdk.contracts.types import ConnectionRef, FileReference, MaxItems
from application_sdk.credentials.ref import CredentialRef
from application_sdk.templates.contracts import ExtractionInput


class AppInputContract(ExtractionInput):
    ignore_orphans: bool = False
    """Drop orphan columns instead of linking them to the first table."""
    indirect_lineage: bool = False
    """Enable indirect lineage parsing in the QI app."""
    qi_node_credential: CredentialRef | None = None
