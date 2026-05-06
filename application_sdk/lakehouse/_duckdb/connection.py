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

``memory_limit`` and ``temp_dir`` are interpolated into ``SET …`` SQL
statements, so they're regex-validated at the entry point. ``threads``
and ``preserve_insertion_order`` are coerced to ``int`` / ``bool``.

Requires the ``[lakehouse-sql]`` install extra. ``duckdb`` is imported
lazily inside :func:`make_duckdb_connection` so apps that only need
``[lakehouse]`` core or ``[lakehouse-bulk]`` don't ImportError on module
load.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

DEFAULT_THREADS = 1
DEFAULT_MEMORY_LIMIT = "8GB"
DEFAULT_TEMP_DIR = "/tmp/duckdb_tmp"
DEFAULT_PRESERVE_INSERTION_ORDER = False

# DuckDB memory_limit accepts e.g. "8GB", "512MB", "1.5GiB". Anything else is
# rejected — these go into a SET statement so a quote/semicolon would be a
# SQL injection vector.
_MEMORY_LIMIT_RE = re.compile(
    r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$",
    re.IGNORECASE,
)


def _import_duckdb():
    """Defer the duckdb import so apps without [lakehouse-sql] don't ImportError."""
    try:
        import duckdb as _duckdb  # noqa: PLC0415

        return _duckdb
    except ImportError as exc:
        raise ImportError(
            "duckdb is required for SQL queries. "
            "Install with: pip install atlan-application-sdk[lakehouse-sql]"
        ) from exc


def _validate_memory_limit(value: str) -> str:
    if not _MEMORY_LIMIT_RE.match(value):
        raise ValueError(
            f"Invalid memory_limit {value!r} — expected e.g. '8GB', '512MB', '1.5GiB'"
        )
    return value


def _validate_temp_dir(value: str) -> str:
    # No quotes or semicolons (SQL escape vector); no path-traversal.
    if any(ch in value for ch in ("'", '"', ";", "\n", "\r")):
        raise ValueError(f"Invalid temp_dir {value!r} — contains disallowed characters")
    # Check the raw input for ``..`` components — normpath() resolves them
    # away, which would let a traversing input slip through.
    if ".." in value.split(os.sep):
        raise ValueError(f"Invalid temp_dir {value!r} — path traversal disallowed")
    return value


def make_duckdb_connection(
    *,
    threads: int = DEFAULT_THREADS,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    temp_dir: str = DEFAULT_TEMP_DIR,
    preserve_insertion_order: bool = DEFAULT_PRESERVE_INSERTION_ORDER,
) -> "duckdb.DuckDBPyConnection":
    """Create a DuckDB in-memory connection with the given tunables.

    All defaults match popularity-app's ``make_duckdb_connection``. Apps
    that need different settings (more threads for parallel scans, larger
    memory limit, alternate spill dir) override the corresponding param.
    """
    _validate_memory_limit(memory_limit)
    _validate_temp_dir(temp_dir)
    duckdb = _import_duckdb()
    os.makedirs(temp_dir, exist_ok=True)
    conn = duckdb.connect()
    conn.execute(f"SET threads TO {int(threads)}")
    conn.execute(f"SET memory_limit = '{memory_limit}'")
    conn.execute(f"SET temp_directory = '{temp_dir}'")
    conn.execute(
        f"SET preserve_insertion_order = {str(bool(preserve_insertion_order)).lower()}"
    )
    return conn
