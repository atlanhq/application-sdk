"""Generic lakehouse reader.

Reads from any namespace the catalog grants the app access to. Has no
polling, no snapshot tracking, no max-duration loop — callers (typically
Temporal activities) drive each read explicitly.
"""

from __future__ import annotations

import logging
from typing import Any

import pyarrow as pa
from pyiceberg.table import Table

from application_sdk.lakehouse.catalog_client import CatalogClient

logger = logging.getLogger(__name__)


class LakehouseReader:
    """Read Iceberg tables via a CatalogClient."""

    def __init__(self, catalog_client: CatalogClient) -> None:
        self._client = catalog_client

    @classmethod
    def from_env(cls) -> LakehouseReader:
        """Build a reader using the catalog credentials from the environment.

        See :func:`application_sdk.lakehouse.catalog_client.load_catalog_from_env`
        for the env vars consumed.
        """
        return cls(CatalogClient.from_env())

    def load_table(self, namespace: str, table_name: str) -> Table:
        return self._client.load_table(namespace, table_name)

    def read_arrow(
        self,
        namespace: str,
        table_name: str,
        row_filter: str | None = None,
        limit: int | None = None,
        selected_fields: tuple[str, ...] | None = None,
    ) -> pa.Table:
        """Scan an Iceberg table and return the result as a PyArrow Table.

        Returns an empty arrow table (preserving the iceberg schema) when the
        table is missing rows for the filter.
        """
        table = self.load_table(namespace, table_name)
        scan = table.scan(
            row_filter=row_filter or "true",
            selected_fields=selected_fields or ("*",),
            limit=limit,
        )
        return scan.to_arrow()

    def fetch_records(
        self,
        namespace: str,
        table_name: str,
        row_filter: str | None = None,
        limit: int | None = None,
        sort_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read a table and return rows as plain dicts.

        Convenience over ``read_arrow`` for callers that want to iterate over
        Python dicts instead of an arrow table. ``sort_by`` is applied in-process
        because Iceberg scans don't guarantee row order.
        """
        arrow = self.read_arrow(
            namespace, table_name, row_filter=row_filter, limit=limit
        )
        if arrow.num_rows == 0:
            return []
        records = arrow.to_pylist()
        if sort_by:
            records.sort(key=lambda r: r.get(sort_by))
        if limit is not None:
            return records[:limit]
        return records

    def current_snapshot_id(self, namespace: str, table_name: str) -> int | None:
        """Return the table's current snapshot id, or None if the table is empty.

        Useful when an app wants to record what version of the data it processed,
        but the reader does not track snapshots itself.
        """
        table = self.load_table(namespace, table_name)
        snapshot = table.current_snapshot()
        return snapshot.snapshot_id if snapshot else None
