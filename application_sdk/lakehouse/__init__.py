"""Lakehouse integration for SDK apps.

Public surface — apps should only import from this module:

* :class:`LakehouseReader` / :class:`LakehouseWriter` — generic primitives.
  Apps work with plain ``dict`` records and an SDK :class:`Schema` for
  record-oriented work, or stage Parquet files at a prefix path and call
  :meth:`LakehouseWriter.append_bulk` for large-batch writes. No
  pyiceberg / pyarrow / daft types appear on the public boundary —
  records as dicts, source paths as strings.

* :class:`EventsConsumer` — event-trigger wrapper for AE-driven apps. The
  caller passes only an async ``process_fn`` callable; the consumer
  self-constructs its lakehouse reader from environment credentials. The
  lakehouse is a blackbox to the events consumer's caller.

* :class:`EventAckWriter` — publishes the AE Parquet ack after a batch.

* :class:`LakehouseQuery` — runs arbitrary SQL across one or more lakehouse
  tables (joins, aggregations, window functions) via DuckDB. Tables are
  Arrow-staged from PyIceberg, registered as views, and queried in-process.
  Returns ``list[dict]``.

* :class:`Schema` / :class:`Field` / :class:`PartitionBy` — SDK dataclasses
  for declaring table shapes without depending on pyiceberg.

When to use what
----------------

  +-----------------------------+---------------------------------------+----------------+
  | App pattern                 | Right tool                            | Install extra  |
  +=============================+=======================================+================+
  | Append small batches with a | ``LakehouseWriter.append`` (PyArrow + | ``[lakehouse]``|
  | typed schema (events,       | PyIceberg under the hood)             |                |
  | audit, ack rows)            |                                       |                |
  +-----------------------------+---------------------------------------+----------------+
  | Append / overwrite large    | ``LakehouseWriter.append_bulk`` —     | ``[lakehouse-  |
  | batches: stage Parquet      | reads Parquet from a prefix path and  | bulk]``        |
  | files at a prefix path      | commits an Iceberg snapshot via Daft  |                |
  +-----------------------------+---------------------------------------+----------------+
  | Read filtered records from  | ``LakehouseReader.fetch_records``     | ``[lakehouse]``|
  | a single table              |                                       |                |
  +-----------------------------+---------------------------------------+----------------+
  | Joins / aggregations /      | ``LakehouseQuery.sql`` (DuckDB        | ``[lakehouse-  |
  | window functions across     | over Arrow-staged views)              | sql]``         |
  | multiple tables             |                                       |                |
  +-----------------------------+---------------------------------------+----------------+
  | Receive an upstream trigger | ``EventsConsumer.handle_events``      | ``[lakehouse]``|
  | and dispatch unprocessed    |                                       |                |
  | events to a process_fn      |                                       |                |
  +-----------------------------+---------------------------------------+----------------+
  | Publish a Parquet ack to AE | ``EventAckWriter.write``              | ``[lakehouse]``|
  | after processing a batch    |                                       |                |
  +-----------------------------+---------------------------------------+----------------+

Install extras
--------------

* ``pip install atlan-application-sdk[lakehouse]`` — core: PyIceberg + PyArrow.
  Covers Reader, Writer.append, EventsConsumer, EventAckWriter, schemas.
* ``pip install atlan-application-sdk[lakehouse-sql]`` — adds DuckDB.
  Required for :class:`LakehouseQuery`.
* ``pip install atlan-application-sdk[lakehouse-bulk]`` — adds Daft.
  Required for :meth:`LakehouseWriter.append_bulk`.

Apps that don't pull the heavy extras still import everything from
``application_sdk.lakehouse`` cleanly — the missing dep raises an
``ImportError`` with a clear install hint only when the corresponding
method is actually called.

Multi-cloud
-----------

The SDK detects the cloud from the ``CLOUD`` env var (``aws`` | ``gcp`` |
``azure``; defaults to ``aws``) and adjusts catalog construction
accordingly:

* **AWS**: PyIceberg's default S3 FileIO. ``AWS_REGION`` is read for the
  S3 region (used by DuckDB write paths and any future ATTACH path).
* **GCP**: PyIceberg's default GCS FileIO. HMAC keys are expected to be
  mapped from the ``gcp-hmac-keys`` k8s secret to ``AWS_*`` env vars by
  the platform Helm chart (DuckDB GCS provider reads them from there).
* **Azure**: catalog is configured with
  ``header.X-Iceberg-Access-Delegation: vended-credentials`` so Polaris
  vends short-lived SAS tokens, and ``py-io-impl: pyiceberg.io.fsspec.
  FsspecFileIO`` so PyIceberg uses ``adlfs``. Daft's Iceberg IO
  converter is patched to normalise account-scoped ADLS keys
  (``adls.sas-token.<account>.dfs.core.windows.net`` → ``adls.sas-token``).

Internal layout
---------------

PyIceberg / pyarrow / Polaris / DuckDB / Daft specifics live in:

* ``_polaris/`` — catalog construction (URL pattern, OAuth scope,
  per-cloud config), Daft ADLS patch, DuckDB per-cloud secret builders.
* ``_iceberg/`` — Iceberg-format ops (identifier, schema mapping,
  scan/append/ensure_table). Generic across IRC implementations.
* ``_duckdb/`` — DuckDB connection factory + Arrow-staged query engine.
* ``_daft/`` — Daft DataFrame → Iceberg writer.

Apps must not import from these underscore-prefixed packages.

"""

from application_sdk.lakehouse.event_ack import EventAckWriter
from application_sdk.lakehouse.events_consumer import EventsConsumer
from application_sdk.lakehouse.models import ProcessingResult
from application_sdk.lakehouse.query import LakehouseQuery
from application_sdk.lakehouse.reader import LakehouseReader
from application_sdk.lakehouse.schema import Field, PartitionBy, Schema
from application_sdk.lakehouse.writer import LakehouseWriter

__all__ = [
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
