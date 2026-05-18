"""Internal: load lakehouse tables to Arrow, run SQL via DuckDB, return dicts.

Arrow-staging strategy: each referenced lakehouse table is fetched via
``pyiceberg`` into an Arrow table once, registered as a DuckDB view, and
queried in-process. This intentionally avoids DuckDB's Iceberg-extension
``ATTACH`` path (which has known multi-table-JOIN credential bugs and
4-part-name parser quirks per the popularity-app design doc).

The SDK does not expose DuckDB connection objects; apps interact only
with SQL strings and ``list[dict]`` results.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
from pyiceberg.catalog import Catalog

from application_sdk.lakehouse._duckdb.connection import (
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_PRESERVE_INSERTION_ORDER,
    DEFAULT_TEMP_DIR,
    DEFAULT_THREADS,
    make_duckdb_connection,
)
from application_sdk.lakehouse._iceberg import identifier as _identifier


def _load_arrow(
    catalog: Catalog,
    namespace: str,
    table_name: str,
    where: str | None,
) -> pa.Table:
    table = catalog.load_table(_identifier.identifier(namespace, table_name))
    return table.scan(row_filter=where or "true").to_arrow()


def run_sql(
    catalog: Catalog,
    query: str,
    tables: dict[str, tuple[str, str]],
    *,
    where: dict[str, str] | None = None,
    threads: int = DEFAULT_THREADS,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    temp_dir: str = DEFAULT_TEMP_DIR,
    preserve_insertion_order: bool = DEFAULT_PRESERVE_INSERTION_ORDER,
) -> list[dict[str, Any]]:
    """Execute ``query`` against ``tables`` registered as DuckDB views.

    ``tables`` maps view names to ``(namespace, table_name)`` tuples. Each
    entry is loaded via PyIceberg into an Arrow table and registered under
    its view name before the query runs. ``where`` lets callers push a
    per-view filter into the Iceberg scan (cheaper than relying on DuckDB
    to filter after materialization).
    """
    conn = make_duckdb_connection(
        threads=threads,
        memory_limit=memory_limit,
        temp_dir=temp_dir,
        preserve_insertion_order=preserve_insertion_order,
    )
    try:
        for view_name, (namespace, table_name) in tables.items():
            view_filter = (where or {}).get(view_name)
            arrow = _load_arrow(catalog, namespace, table_name, view_filter)
            conn.register(view_name, arrow)
        result = conn.execute(query)
        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()
