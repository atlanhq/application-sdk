"""Lakehouse integration for SDK apps.

Two public surfaces:

* :class:`LakehouseReader` and :class:`LakehouseWriter` — generic Iceberg
  read/write primitives. ``LakehouseReader`` reads from any namespace.
  ``LakehouseWriter`` is bound to one ``app_namespace`` and warns on
  cross-namespace writes. Both ship ``.from_env()`` factories.

* :class:`EventsConsumer` — an event-trigger wrapper for AE-driven apps. The
  caller passes only a :class:`BatchProcessor`; the consumer self-constructs
  its lakehouse reader from environment credentials. The lakehouse is a
  blackbox to the events consumer's caller.

For publishing the AE Parquet ack after a batch, use :class:`EventAckWriter`.

Install with: ``pip install atlan-application-sdk[lakehouse]``
"""

from application_sdk.lakehouse.catalog_client import (
    CatalogClient,
    load_catalog_from_env,
)
from application_sdk.lakehouse.event_ack import EventAckWriter
from application_sdk.lakehouse.events_consumer import EventsConsumer
from application_sdk.lakehouse.models import ProcessingResult
from application_sdk.lakehouse.protocols import BatchProcessor
from application_sdk.lakehouse.reader import LakehouseReader
from application_sdk.lakehouse.writer import LakehouseWriter

__all__ = [
    "BatchProcessor",
    "CatalogClient",
    "EventAckWriter",
    "EventsConsumer",
    "LakehouseReader",
    "LakehouseWriter",
    "ProcessingResult",
    "load_catalog_from_env",
]
