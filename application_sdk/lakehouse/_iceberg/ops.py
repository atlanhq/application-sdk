"""Internal: scan / append / ensure-table primitives implemented on pyiceberg.

These functions are the only place we touch ``pyiceberg.table.Table`` or
``pyarrow.Table``. ``LakehouseReader`` and ``LakehouseWriter`` call into them
with plain dicts and SDK :class:`Schema` instances; nothing pyiceberg-shaped
crosses the public boundary.
"""

from __future__ import annotations

import logging
from typing import Any

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.table import Table

from application_sdk.lakehouse._iceberg import identifier as _identifier
from application_sdk.lakehouse._iceberg import schema_mapper as _mapper
from application_sdk.lakehouse.schema import Schema

logger = logging.getLogger(__name__)


def scan_records(
    catalog: Catalog,
    namespace: str,
    table_name: str,
    *,
    where: str | None = None,
    limit: int | None = None,
    select: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Scan a table and return rows as plain dicts."""
    table = catalog.load_table(_identifier.identifier(namespace, table_name))
    scan = table.scan(
        row_filter=where or "true",
        selected_fields=select or ("*",),
        limit=limit,
    )
    arrow = scan.to_arrow()
    if arrow.num_rows == 0:
        return []
    return arrow.to_pylist()


def current_snapshot_id(
    catalog: Catalog,
    namespace: str,
    table_name: str,
) -> int | None:
    table = catalog.load_table(_identifier.identifier(namespace, table_name))
    snapshot = table.current_snapshot()
    return snapshot.snapshot_id if snapshot else None


def ensure_table(
    catalog: Catalog,
    namespace: str,
    table_name: str,
    schema: Schema,
) -> Table:
    """Load the table, creating it (and its namespace) from the SDK schema if missing."""
    identifier = _identifier.identifier(namespace, table_name)
    try:
        return catalog.load_table(identifier)
    except (NoSuchTableError, Exception):
        _identifier.ensure_namespace(catalog, namespace)
        iceberg_schema = _mapper.to_iceberg_schema(schema)
        partition_spec = _mapper.to_partition_spec(schema, iceberg_schema)
        kwargs: dict[str, Any] = {
            "identifier": identifier,
            "schema": iceberg_schema,
        }
        if partition_spec.fields:
            kwargs["partition_spec"] = partition_spec
        return catalog.create_table(**kwargs)


def append_records(
    catalog: Catalog,
    namespace: str,
    table_name: str,
    records: list[dict[str, Any]],
    *,
    schema: Schema | None,
) -> int:
    """Append records to an Iceberg table.

    If ``schema`` is provided, the table is created if missing using that
    schema. If ``schema`` is None, the table must already exist.
    """
    if not records:
        return 0
    if schema is not None:
        table = ensure_table(catalog, namespace, table_name, schema)
        arrow_schema = _mapper.to_arrow_schema(schema)
    else:
        table = catalog.load_table(_identifier.identifier(namespace, table_name))
        arrow_schema = table.schema().as_arrow()
    arrow_table = pa.Table.from_pylist(records, schema=arrow_schema)
    table.append(arrow_table)
    return arrow_table.num_rows


def table_exists(catalog: Catalog, namespace: str, table_name: str) -> bool:
    try:
        catalog.load_table(_identifier.identifier(namespace, table_name))
        return True
    except Exception:
        return False
