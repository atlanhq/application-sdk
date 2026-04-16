"""Lakehouse consumer framework for event-driven metadata actions.

Provides a poll-based consumer that reads events from an Iceberg table
(written by the automation-engine event pipeline), delegates processing
to a source-app-provided BatchProcessor, and writes OUTCOME rows back.

Install with: pip install atlan-application-sdk[lakehouse]
"""

from application_sdk.lakehouse.models import ProcessingResult
from application_sdk.lakehouse.protocols import BatchProcessor

__all__ = [
    "BatchProcessor",
    "ProcessingResult",
]
