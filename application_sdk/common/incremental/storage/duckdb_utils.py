"""DuckDB utilities for incremental metadata extraction.

Provides DuckDBConnectionManager for file-backed connections with auto cleanup,
managed_duckdb_connection for optional connection reuse in helper functions,
and SQL helpers like escape_sql_string, json_scan, and get_parent_table_qn_expr.
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from collections.abc import Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeAlias

from application_sdk._runtime.offload import current_cancel_handle
from application_sdk.constants import (
    DUCKDB_COMMON_TEMP_FOLDER,
    DUCKDB_DEFAULT_MEMORY_LIMIT,
    DUCKDB_MAX_MEMORY_LIMIT_BYTES,
    DUCKDB_MEMORY_LIMIT_FRACTION,
    DUCKDB_MEMORY_LIMIT_OVERRIDE,
    DUCKDB_MIN_MEMORY_LIMIT_BYTES,
)
from application_sdk.observability import cgroup as _cgroup
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = get_logger(__name__)

try:
    import duckdb
except ImportError:  # conformance: ignore[E008,E009] optional dep duckdb not installed; sentinel fallback
    duckdb = None  # type: ignore[assignment]


class DuckDBResult(Protocol):
    """The result side of a DuckDB connection: what ``execute`` hands back.

    Rows are ``tuple[object, ...]``: DuckDB types a column only at run time, so
    a caller narrows each value it reads.
    """

    def fetchall(self) -> list[tuple[object, ...]]: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


class DuckDBConnectionLike(Protocol):
    """The subset of ``duckdb.DuckDBPyConnection`` the incremental helpers call.

    Structural, so a real connection satisfies it without importing
    ``duckdb`` (an optional dependency) at type-check time, and a test can
    pass a fake.
    """

    def execute(self, query: str, parameters: object = None, /) -> DuckDBResult: ...

    def executemany(self, query: str, parameters: object = None, /) -> DuckDBResult: ...

    def close(self) -> None: ...


#: Optional connection parameter type for helpers that can reuse a caller's
#: connection or open their own (see :func:`managed_duckdb_connection`).
DuckDBConnection: TypeAlias = DuckDBConnectionLike | None


def _generate_random_uuid(length: int = 8) -> str:
    """Generate a short random UUID string."""
    return uuid.uuid4().hex[:length]


def interrupt_on_cancel(conn: DuckDBPyConnection) -> AbstractContextManager[None]:
    """Within the block, interrupt *conn*'s query if this ``run_in_thread`` call is abandoned.

    Registers ``conn.interrupt`` — safe to call from another thread — as the
    call's driver cancel, and puts back whatever was registered before when
    the block ends, so a helper that opens its own connection mid-scan does not
    leave the caller's connection uncancellable. Without it a cancelled scan
    runs to completion on a thread nobody is waiting for, holding its memory
    and spill files, and a caller that has moved on may close the connection
    under it. A no-op outside ``run_in_thread``.

    Args:
        conn: The connection the offloaded call is about to query.

    Returns:
        A context manager scoping the registration.
    """
    handle = current_cancel_handle()
    if handle is None:
        return nullcontext()
    return handle.registered(conn.interrupt)


#: Open :class:`DuckDBConnectionManager` instances in this process. A new
#: default-sized connection shares the free memory with them.
_open_managers = 0
_open_managers_lock = threading.Lock()


def default_duckdb_memory_limit(open_connections: int = 0) -> str:
    """DuckDB memory limit for a connection opened without an explicit one.

    ``ATLAN_DUCKDB_MEMORY_LIMIT`` wins when set. Otherwise the limit is
    ``DUCKDB_MEMORY_LIMIT_FRACTION`` of the container's free memory (its
    memory limit minus current usage), split evenly between this connection
    and the *open_connections* already open in the process, then clamped to
    ``[DUCKDB_MIN_MEMORY_LIMIT_BYTES, DUCKDB_MAX_MEMORY_LIMIT_BYTES]``. With
    no container limit known it is ``DUCKDB_DEFAULT_MEMORY_LIMIT``; with the
    limit known but usage not, the whole limit counts as free.

    This lowers the chance of an OOM kill; it cannot rule one out. The free
    memory is a snapshot at open time, the floor can exceed what is free on
    an almost-full container, and DuckDB allocates a little outside its
    limit. The reserve left by the fraction absorbs the usual worker growth.

    A fixed 2GB limit is larger than a 2 GiB container that is already
    holding the worker's own state, so DuckDB never spilled and the kernel
    OOM-killed the worker mid-scan instead.

    Args:
        open_connections: DuckDB connections already open in this process,
            each of which may still grow into the same free memory.

    Returns:
        A DuckDB size string, e.g. ``"199MiB"``.
    """
    if DUCKDB_MEMORY_LIMIT_OVERRIDE:
        return DUCKDB_MEMORY_LIMIT_OVERRIDE
    container_limit = _cgroup.memory_limit_bytes()
    if container_limit is None:
        return DUCKDB_DEFAULT_MEMORY_LIMIT
    usage = _cgroup.memory_usage_bytes()
    free = max(0, container_limit - (usage or 0))
    share = int(free * DUCKDB_MEMORY_LIMIT_FRACTION) // (max(0, open_connections) + 1)
    limit = min(
        DUCKDB_MAX_MEMORY_LIMIT_BYTES, max(DUCKDB_MIN_MEMORY_LIMIT_BYTES, share)
    )
    return f"{limit // (1024 * 1024)}MiB"


def _release_manager_slot() -> None:
    global _open_managers  # noqa: PLW0603
    with _open_managers_lock:
        _open_managers = max(0, _open_managers - 1)


class DuckDBConnectionManager:
    """File-backed DuckDB connection with automatic cleanup.

    Each instance creates a unique directory under base_path,
    safe for concurrent workflows on the same pod.
    Use as a context manager for automatic resource cleanup.
    """

    def __init__(
        self,
        base_path: str = DUCKDB_COMMON_TEMP_FOLDER,
        memory_limit: str | None = None,
        threads: str = "1",
        auto_cleanup: bool = True,
    ):
        """Initialize file-backed DuckDB connection.

        Args:
            base_path: Parent directory for DuckDB instances
            memory_limit: DuckDB memory limit. Default: a share of the
                container's free memory, split with the other open managers,
                by :func:`default_duckdb_memory_limit` (at most 2GB).
            threads: Number of threads (default: 1)
            auto_cleanup: Remove temp files on close (default: True)
        """
        if duckdb is None:
            raise ImportError("duckdb is required for DuckDBConnectionManager")

        global _open_managers  # noqa: PLW0603
        with _open_managers_lock:
            if memory_limit is None:
                memory_limit = default_duckdb_memory_limit(_open_managers)
            _open_managers += 1

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

        try:
            os.makedirs(self._instance_path, exist_ok=True)
            os.makedirs(self._temp_dir, exist_ok=True)

            logger.info(
                "Creating DuckDB: file=%s memory_limit=%s", self._db_file, memory_limit
            )
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
        except BaseException:
            # Never opened, so close() will not run: give the slot back.
            _release_manager_slot()
            raise
        # A manager opened inside an offloaded call (the common case: the
        # helper opens its own connection) is interrupted when that call is,
        # until it closes.
        self._interrupt_scope = ExitStack()
        self._interrupt_scope.enter_context(interrupt_on_cancel(self._connection))

    @property
    def connection(self) -> DuckDBPyConnection:
        """Get the underlying DuckDB connection."""
        if self._is_closed:
            from application_sdk.common.incremental.incremental_errors import (  # noqa: PLC0415
                DuckDbConnectionClosedError,
            )

            raise DuckDbConnectionClosedError()
        return self._connection

    def close(self) -> None:
        """Close connection and cleanup resources."""
        if self._is_closed:
            return

        # First, so a cancel landing now reaches the enclosing scope's
        # connection rather than this closing one.
        self._interrupt_scope.close()
        try:
            self._connection.close()
            logger.info("DuckDB connection closed")
        except Exception:
            logger.warning("Error closing DuckDB", exc_info=True)
        finally:
            self._is_closed = True
            _release_manager_slot()
            if self._auto_cleanup:
                shutil.rmtree(self._instance_path, ignore_errors=True)

    def __enter__(self) -> DuckDBConnectionManager:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@contextmanager
def managed_duckdb_connection(
    conn: DuckDBConnection = None,
) -> Generator[DuckDBConnectionLike, None, None]:
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
    active_conn: DuckDBConnectionLike = conn if conn else duckdb.connect(":memory:")

    try:
        yield active_conn
    finally:
        if owns_connection and active_conn:
            active_conn.close()


# =============================================================================
# SQL Helpers
# =============================================================================


def fetch_count(result: DuckDBResult) -> int:
    """Read a ``SELECT COUNT(*)`` result: its one value, or 0 when there is no row.

    Raises:
        TypeError: If the first column is not an integer (the query was not a
            count).
    """
    row = result.fetchone()
    if row is None:
        return 0
    value = row[0]
    if not isinstance(value, int):
        raise TypeError(f"expected an integer count, got {type(value).__name__}")
    return value


def fetch_str_set(result: DuckDBResult) -> set[str]:
    """Collect the first column of every row as a set of strings, skipping NULLs."""
    return {row[0] for row in result.fetchall() if isinstance(row[0], str)}


def escape_sql_string(value: str) -> str:
    """Escape single quotes for safe SQL embedding."""
    return value.replace("'", "''")


def json_scan(files: list[Path] | list[str]) -> str:
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
