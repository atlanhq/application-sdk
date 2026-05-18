"""Generic lakehouse reader.

Reads from any namespace the catalog grants the app access to. Apps interact
with plain ``dict`` records — no Iceberg or Arrow types appear on the public
surface; the internal :mod:`_iceberg` package handles that.
"""

from __future__ import annotations

from typing import Any

from application_sdk.lakehouse._iceberg import ops as _ops
from application_sdk.lakehouse._polaris import catalog as _catalog


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

        When ``sort_by`` is provided, ``limit`` is **not** pushed into the
        Iceberg scan — the SDK reads the full filtered result, sorts in
        process, then slices. Pushing ``limit`` to the scan in combination
        with an in-process sort would return "the first N rows the scanner
        emitted, sorted" rather than "the N smallest by ``sort_by``".

        Example::

            from application_sdk.lakehouse import LakehouseReader

            reader = LakehouseReader.from_env()

            # Filtered + sorted + limited read of pending events
            events = reader.fetch_records(
                "automation_engine", "reverse_sync_description",
                where="status = 'unprocessed'",
                sort_by="received_at",
                limit=5000,
            )
            # → list[dict] with keys: event_id, payload, received_at, ...

            # Project a subset of columns
            ids = reader.fetch_records(
                "automation_engine", "reverse_sync_description",
                select=("event_id", "received_at"),
            )
        """
        # Push limit to the scan only when no in-process sort is needed; with
        # sort_by, we must see the full filtered set before slicing.
        scan_limit = None if sort_by else limit
        records = _ops.scan_records(
            self._catalog,
            namespace,
            table_name,
            where=where,
            limit=scan_limit,
            select=select,
        )
        if sort_by:
            records.sort(key=lambda r: r.get(sort_by))
        if limit is not None:
            return records[:limit]
        return records

    def current_snapshot_id(self, namespace: str, table_name: str) -> int | None:
        """Return the table's current snapshot id, or None if the table is empty.

        Useful for recording exactly which version of the data a workflow
        processed (e.g., for incremental cursors or audit trails).

        Example::

            snap = reader.current_snapshot_id("automation_engine", "events")
            if snap and snap == last_processed_snap:
                return  # nothing new
        """
        return _ops.current_snapshot_id(self._catalog, namespace, table_name)
