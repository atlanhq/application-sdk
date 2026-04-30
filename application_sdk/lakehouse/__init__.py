"""Lakehouse integration for SDK apps.

Apps construct a :class:`LakehouseInterface` once with their catalog and the
namespace they own. The interface bundles a generic :class:`LakehouseReader`
(read from any namespace) and a namespace-bound :class:`LakehouseWriter`
(writes target the app's own namespace; cross-namespace writes log a
warning).

For event-driven apps triggered by the automation engine, use
:class:`EventTriggeredConsumer` to fetch + dispatch in one shot from a
Temporal activity, and :class:`EventAckWriter` to publish the Parquet ack
back to AE.

Install with: ``pip install atlan-application-sdk[lakehouse]``
"""

from application_sdk.lakehouse.catalog_client import CatalogClient
from application_sdk.lakehouse.event_ack import EventAckWriter
from application_sdk.lakehouse.event_consumer import EventTriggeredConsumer
from application_sdk.lakehouse.interface import LakehouseInterface
from application_sdk.lakehouse.models import ProcessingResult
from application_sdk.lakehouse.protocols import BatchProcessor
from application_sdk.lakehouse.reader import LakehouseReader
from application_sdk.lakehouse.writer import LakehouseWriter

__all__ = [
    "BatchProcessor",
    "CatalogClient",
    "EventAckWriter",
    "EventTriggeredConsumer",
    "LakehouseInterface",
    "LakehouseReader",
    "LakehouseWriter",
    "ProcessingResult",
]
