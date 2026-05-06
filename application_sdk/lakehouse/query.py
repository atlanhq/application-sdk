"""Run arbitrary SQL against lakehouse tables.

Apps register one or more lakehouse tables under view names, then run any
DuckDB-compatible SQL against them. Tables are loaded via PyIceberg into
Arrow once per call, registered as DuckDB views, and the query executes
in-process. The SDK returns plain ``list[dict]``; no DuckDB or PyIceberg
types are exposed on the public boundary.

Construction is env-driven via :meth:`from_env`. Catalog credentials follow
the same ``ICEBERG_*`` / ``ATLAN_DOMAIN_NAME`` convention used by
:class:`LakehouseReader` and :class:`LakehouseWriter`.

Example::

    from application_sdk.lakehouse import LakehouseQuery

    q = LakehouseQuery.from_env()
    rows = q.sql(
        '''
        SELECT e.event_id, a.qualified_name
        FROM events e
        JOIN assets a ON e.asset_id = a.guid
        WHERE e.status = 'unprocessed'
        ''',
        tables={
            "events": ("automation_engine", "events_table"),
            "assets": ("gold", "relational_asset_details"),
        },
    )

DuckDB tunables (``threads``, ``memory_limit``, ``temp_dir``,
``preserve_insertion_order``) default to popularity-app's settings and can
be overridden per-call.

This sits alongside :class:`LakehouseReader` / :class:`LakehouseWriter` and
is the recommended path when an app needs joins, aggregations, or window
functions across lakehouse tables.
"""

from __future__ import annotations

from typing import Any

from application_sdk.lakehouse._duckdb import query_engine as _engine
from application_sdk.lakehouse._duckdb.connection import (
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_PRESERVE_INSERTION_ORDER,
    DEFAULT_TEMP_DIR,
    DEFAULT_THREADS,
)
from application_sdk.lakehouse._polaris import catalog as _catalog


class LakehouseQuery:
    """Run SQL across lakehouse tables and return ``list[dict]`` results."""

    def __init__(self, _catalog_obj: Any) -> None:
        self._catalog = _catalog_obj

    @classmethod
    def from_env(cls) -> LakehouseQuery:
        """Build a query engine using catalog credentials from the environment."""
        return cls(_catalog.load_catalog_from_env())

    def sql(
        self,
        query: str,
        *,
        tables: dict[str, tuple[str, str]],
        where: dict[str, str] | None = None,
        threads: int = DEFAULT_THREADS,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        temp_dir: str = DEFAULT_TEMP_DIR,
        preserve_insertion_order: bool = DEFAULT_PRESERVE_INSERTION_ORDER,
    ) -> list[dict[str, Any]]:
        """Execute ``query`` against named lakehouse tables. Returns rows as dicts.

        Args:
            query: Any DuckDB-compatible SQL referencing the view names from
                ``tables``.
            tables: View name → ``(namespace, table_name)``. Each table is
                materialized to Arrow via PyIceberg and registered under the
                view name before the query runs.
            where: Optional per-view Iceberg row-filter applied at scan time
                (e.g. ``{"events": "status = 'unprocessed'"}``). Cheaper than
                filtering inside the SQL when partition pruning helps.
            threads: DuckDB ``threads`` setting. Default ``1`` (popularity-app
                convention; bump for parallel scans on dedicated workers).
            memory_limit: DuckDB ``memory_limit``. Default ``'8GB'``.
            temp_dir: DuckDB spill directory. Default ``/tmp/duckdb_tmp``.
            preserve_insertion_order: DuckDB ``preserve_insertion_order``.
                Default ``False`` (cheaper sorts/aggregations).
        """
        return _engine.run_sql(
            self._catalog,
            query,
            tables,
            where=where,
            threads=threads,
            memory_limit=memory_limit,
            temp_dir=temp_dir,
            preserve_insertion_order=preserve_insertion_order,
        )
