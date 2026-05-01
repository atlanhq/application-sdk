"""Lakehouse integration for SDK apps.

Public surface — apps should only import from this module:

* :class:`LakehouseReader` and :class:`LakehouseWriter` — generic primitives.
  Apps work with plain ``dict`` records and an SDK :class:`Schema` declaration.
  No Iceberg or Arrow types appear on the public boundary.

* :class:`EventsConsumer` — event-trigger wrapper for AE-driven apps. The
  caller passes only a :class:`BatchProcessor`; the consumer self-constructs
  its lakehouse reader from environment credentials. The lakehouse is a
  blackbox to the events consumer's caller.

* :class:`EventAckWriter` — publishes the AE Parquet ack after a batch.

* :class:`LakehouseQuery` — runs arbitrary SQL across one or more lakehouse
  tables (joins, aggregations, window functions) and returns ``list[dict]``.

* :class:`Schema` / :class:`Field` / :class:`PartitionBy` — SDK dataclasses
  for declaring table shapes without depending on pyiceberg.

The PyIceberg / Polaris / DuckDB specifics live in
``application_sdk.lakehouse._iceberg``,
``application_sdk.lakehouse._polaris``, and
``application_sdk.lakehouse._duckdb`` respectively, and are not part of
the public API.

Install with: ``pip install atlan-application-sdk[lakehouse]``
"""

from application_sdk.lakehouse.event_ack import EventAckWriter
from application_sdk.lakehouse.events_consumer import EventsConsumer
from application_sdk.lakehouse.models import ProcessingResult
from application_sdk.lakehouse.protocols import BatchProcessor
from application_sdk.lakehouse.query import LakehouseQuery
from application_sdk.lakehouse.reader import LakehouseReader
from application_sdk.lakehouse.schema import Field, PartitionBy, Schema
from application_sdk.lakehouse.writer import LakehouseWriter

__all__ = [
    "BatchProcessor",
    "EventAckWriter",
    "EventsConsumer",
    "Field",
    "LakehouseQuery",
    "LakehouseReader",
    "LakehouseWriter",
    "PartitionBy",
    "ProcessingResult",
    "Schema",
]
