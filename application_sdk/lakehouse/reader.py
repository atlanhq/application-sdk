"""Generic lakehouse reader.

Reads from any namespace the catalog grants the app access to. Apps interact
with plain ``dict`` records — no Iceberg or Arrow types appear on the public
surface; the internal :mod:`_iceberg` package handles that.
"""

from __future__ import annotations

from typing import Any

from application_sdk.lakehouse._iceberg import catalog as _catalog
from application_sdk.lakehouse._iceberg import ops as _ops


class LakehouseReader:
    """Read records from any namespace the app has access to."""

    def __init__(self, _catalog_obj: Any) -> None:
        # The catalog object is intentionally untyped on the boundary —
        # apps construct readers via ``from_env`` and never pass it directly.
        self._catalog = _catalog_obj

    @classmethod
    def from_env(cls) -> LakehouseReader:
        """Build a reader from environment credentials.

        Reads ``ICEBERG_CATALOG_URI`` (or derives from ``ATLAN_DOMAIN_NAME``),
        ``ICEBERG_CLIENT_ID``, ``ICEBERG_CLIENT_SECRET``, and the optional
        ``ICEBERG_WAREHOUSE``. Raises ``RuntimeError`` if a required var is
        missing.
        """
        return cls(_catalog.load_catalog_from_env())

    def fetch_records(
        self,
        namespace: str,
        table_name: str,
        *,
        where: str | None = None,
        limit: int | None = None,
        sort_by: str | None = None,
        select: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Read a table and return rows as plain dicts.

        Args:
            namespace: Source namespace (dotted form OK, e.g. ``"apps.databricks"``).
            table_name: Table within the namespace.
            where: Optional Iceberg row-filter expression
                (e.g. ``"status = 'unprocessed'"``). ``None`` reads the full table.
            limit: Optional maximum number of rows to return.
            sort_by: Optional column to sort by ascending. Sort happens
                in-process because Iceberg scans don't guarantee order.
            select: Optional tuple of column names to project.
        """
        records = _ops.scan_records(
            self._catalog,
            namespace,
            table_name,
            where=where,
            limit=limit,
            select=select,
        )
        if sort_by:
            records.sort(key=lambda r: r.get(sort_by))
        if limit is not None:
            return records[:limit]
        return records

    def current_snapshot_id(self, namespace: str, table_name: str) -> int | None:
        """Return the table's current snapshot id, or None if the table is empty."""
        return _ops.current_snapshot_id(self._catalog, namespace, table_name)
