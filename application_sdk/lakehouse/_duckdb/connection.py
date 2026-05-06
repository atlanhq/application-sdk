"""Internal: DuckDB connection factory.

Defaults mirror ``atlan-popularity-app``'s ``make_duckdb_connection`` so all
SDK apps using DuckDB have identical operational characteristics out of the
box:

  * ``threads=1`` — predictable memory on shared workers
  * ``memory_limit='8GB'`` — same upper bound popularity uses
  * ``preserve_insertion_order=False`` — cheaper sorts/aggregations
  * ``temp_dir='/tmp/duckdb_tmp'`` — dedicated spill directory

All four are tunable per-call. Public callers (``LakehouseQuery.sql``)
forward overrides through.
"""

from __future__ import annotations

import os

import duckdb

DEFAULT_THREADS = 1
DEFAULT_MEMORY_LIMIT = "8GB"
DEFAULT_TEMP_DIR = "/tmp/duckdb_tmp"
DEFAULT_PRESERVE_INSERTION_ORDER = False


def make_duckdb_connection(
    *,
    threads: int = DEFAULT_THREADS,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    temp_dir: str = DEFAULT_TEMP_DIR,
    preserve_insertion_order: bool = DEFAULT_PRESERVE_INSERTION_ORDER,
) -> duckdb.DuckDBPyConnection:
    """Create a DuckDB in-memory connection with the given tunables.

    All defaults match popularity-app's ``make_duckdb_connection``. Apps
    that need different settings (more threads for parallel scans, larger
    memory limit, alternate spill dir) override the corresponding param.
    """
    os.makedirs(temp_dir, exist_ok=True)
    conn = duckdb.connect()
    conn.execute(f"SET threads TO {int(threads)}")
    conn.execute(f"SET memory_limit = '{memory_limit}'")
    conn.execute(f"SET temp_directory = '{temp_dir}'")
    conn.execute(
        f"SET preserve_insertion_order = {str(bool(preserve_insertion_order)).lower()}"
    )
    return conn
