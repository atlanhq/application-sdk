"""DuckDB utilities for incremental metadata extraction.

Provides DuckDBConnectionManager for file-backed connections with auto cleanup,
managed_duckdb_connection for optional connection reuse in helper functions,
and SQL helpers like escape_sql_string, json_scan, and get_parent_table_qn_expr.
"""

from __future__ import annotations

import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, List, Optional, Union

from application_sdk.constants import (
    DUCKDB_COMMON_TEMP_FOLDER,
    DUCKDB_DEFAULT_MEMORY_LIMIT,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

try:
    import duckdb

    DuckDBConnection = Optional[duckdb.DuckDBPyConnection]
except ImportError:
    duckdb = None  # type: ignore[assignment]
    DuckDBConnection = Any


def _generate_random_uuid(length: int = 8) -> str:
    """Generate a short random UUID string."""
    return uuid.uuid4().hex[:length]


class DuckDBConnectionManager:
    """File-backed DuckDB connection with automatic cleanup.

    Each instance creates a unique directory under base_path,
    safe for concurrent workflows on the same pod.
    Use as a context manager for automatic resource cleanup.
    """

    def __init__(
        self,
        base_path: str = DUCKDB_COMMON_TEMP_FOLDER,
        memory_limit: str = DUCKDB_DEFAULT_MEMORY_LIMIT,
        threads: str = "1",
        auto_cleanup: bool = True,
    ):
        """Initialize file-backed DuckDB connection.

        Args:
            base_path: Parent directory for DuckDB instances
            memory_limit: DuckDB memory limit (default: 2GB)
            threads: Number of threads (default: 1)
            auto_cleanup: Remove temp files on close (default: True)
        """
        if duckdb is None:
            raise ImportError("duckdb is required for DuckDBConnectionManager")

        self._instance_id = _generate_random_uuid()
        self._base_path = base_path
        self._memory_limit = memory_limit
        self._threads = threads
        self._auto_cleanup = auto_cleanup
        self._is_closed = False

        # Create unique instance directory
        self._instance_path = f"{base_path}/{self._instance_id}"
        self._temp_dir = f"{self._instance_path}/duckdb_temp"
        self._db_file = f"{self._instance_path}/duckdb.db"

        os.makedirs(self._instance_path, exist_ok=True)
        os.makedirs(self._temp_dir, exist_ok=True)

        logger.info(f"Creating DuckDB: {self._db_file} (memory_limit={memory_limit})")
        self._connection = duckdb.connect(
            self._db_file,
            config={
                "threads": threads,
                "memory_limit": memory_limit,
                "temp_directory": self._temp_dir,
                "preserve_insertion_order": "false",
                "enable_object_cache": "false",
            },
            read_only=False,
        )

    @property
    def connection(self) -> "duckdb.DuckDBPyConnection":
        """Get the underlying DuckDB connection."""
        if self._is_closed:
            raise RuntimeError("DuckDB connection has been closed")
        return self._connection

    def close(self) -> None:
        """Close connection and cleanup resources."""
        if self._is_closed:
            return

        try:
            self._connection.close()
            logger.info("DuckDB connection closed")
        except Exception as e:
            logger.warning(f"Error closing DuckDB: {e}")
        finally:
            self._is_closed = True
            if self._auto_cleanup:
                shutil.rmtree(self._instance_path, ignore_errors=True)

    def __enter__(self) -> "DuckDBConnectionManager":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


@contextmanager
def managed_duckdb_connection(
    conn: DuckDBConnection = None,
) -> Generator[Any, None, None]:
    """Context manager for optional connection reuse.

    Used by helper functions that can work standalone OR with a shared connection.

    Args:
        conn: If provided, uses it (caller owns it). If None, creates in-memory.

    Yields:
        DuckDB connection
    """
    if duckdb is None:
        raise ImportError("duckdb is required for managed_duckdb_connection")

    owns_connection = conn is None
    active_conn = conn if conn else duckdb.connect(":memory:")

    try:
        yield active_conn
    finally:
        if owns_connection and active_conn:
            active_conn.close()


# =============================================================================
# SQL Helpers
# =============================================================================


def escape_sql_string(value: str) -> str:
    """Escape single quotes for safe SQL embedding."""
    return value.replace("'", "''")


def json_scan(files: Union[List[Path], List[str]]) -> str:
    """Generate DuckDB read_json_auto SQL fragment.

    Args:
        files: List of file paths to scan

    Returns:
        SQL fragment for reading JSON files
    """
    quoted = ", ".join(f"'{escape_sql_string(str(f))}'" for f in files)
    return f"""
    (
        SELECT
            * EXCLUDE (customAttributes),
            CASE
                WHEN customAttributes IS NULL THEN NULL
                WHEN json_valid(customAttributes) THEN json(customAttributes)
                ELSE json(json_extract(customAttributes, '$'))
            END AS customAttributes
        FROM read_json_auto(
            [{quoted}],
            format='newline_delimited',
            ignore_errors=true
        )
    )
    """


def get_parent_table_qn_expr() -> str:
    """SQL expression to extract parent table QN from column JSON.

    Returns:
        SQL expression that extracts tableQualifiedName or viewQualifiedName
    """
    return """COALESCE(
        json_extract_string(to_json(attributes), '$.tableQualifiedName'),
        json_extract_string(to_json(attributes), '$.viewQualifiedName')
    )"""
