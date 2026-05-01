"""Internal: DuckDB connection factory.

Mirrors the conventions established by ``atlan-popularity-app``'s
``app/processing/io.py:make_duckdb_connection`` so that all SDK apps that
use DuckDB have identical operational characteristics:

  * single-threaded (predictable memory; activities run on shared workers)
  * ``memory_limit`` capped at 8 GB (same upper bound popularity uses)
  * ``preserve_insertion_order=false`` for cheaper sorts/aggregations
  * dedicated ``temp_directory`` so spill files don't collide

If we ever need to make these tunable, do it here — apps should not be
configuring DuckDB themselves.
"""

from __future__ import annotations

import os

import duckdb

_DEFAULT_TEMP_DIR = "/tmp/duckdb_tmp"


def make_duckdb_connection(
    temp_dir: str = _DEFAULT_TEMP_DIR,
) -> duckdb.DuckDBPyConnection:
    """Create a DuckDB in-memory connection configured to popularity-app's tunables."""
    os.makedirs(temp_dir, exist_ok=True)
    conn = duckdb.connect()
    conn.execute("SET threads TO 1")
    conn.execute("SET memory_limit = '8GB'")
    conn.execute(f"SET temp_directory = '{temp_dir}'")
    conn.execute("SET preserve_insertion_order = false")
    return conn
