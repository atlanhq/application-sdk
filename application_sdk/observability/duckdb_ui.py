"""Thin DuckDB UI module to avoid loading pandas/dapr at API server startup.

This module contains only DuckDBUI and minimal deps (os, pathlib, constants).
Importing from here does not pull in application_sdk.observability.observability.
"""

import os
from pathlib import Path

from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_NAME,
    OBSERVABILITY_DIR,
    TEMPORARY_PATH,
)


def _get_observability_dir() -> str:
    """Build the observability path (mirrors observability.utils.get_observability_dir)."""
    return os.path.join(
        TEMPORARY_PATH,
        OBSERVABILITY_DIR.format(
            application_name=APPLICATION_NAME, deployment_name=DEPLOYMENT_NAME
        ),
    )


class DuckDBUI:
    """Class to handle DuckDB UI functionality."""

    def __init__(self):
        """Initialize the DuckDB UI handler."""
        self.observability_dir = _get_observability_dir()
        self.db_path = self.observability_dir + "/observability.db"
        self._duckdb_ui_con = None

    def _is_duckdb_ui_running(self, host: str = "0.0.0.0", port: int = 4213) -> bool:
        """Check if DuckDB UI is already running on the default port."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            result = sock.connect_ex((host, port))
            return result == 0

    def start_ui(self) -> None:
        """Start DuckDB UI and create views for Hive partitioned parquet files."""
        import duckdb  # Lazy import to avoid loading DuckDB at API server startup

        if not self._is_duckdb_ui_running():
            os.makedirs(self.observability_dir, exist_ok=True)
            con = duckdb.connect(self.db_path)

            def process_partitioned_files(directory: str, prefix: str = "") -> None:
                """Process Hive partitioned parquet files and create views."""
                if not os.path.exists(directory):
                    return
                if not any(Path(directory).rglob("*.parquet")):
                    return
                view_name = prefix if prefix else "data"
                view_query = f"""
                CREATE OR REPLACE VIEW {view_name} AS
                SELECT *
                FROM read_parquet('{directory}/**/*.parquet',
                                hive_partitioning = true,
                                hive_types = {{'year': INTEGER, 'month': INTEGER, 'day': INTEGER}})
                """
                con.execute(view_query)

            for data_type in ["logs", "metrics", "traces"]:
                data_dir = os.path.join(self.observability_dir, data_type)
                if os.path.exists(data_dir):
                    process_partitioned_files(data_dir, data_type)

            con.execute("CALL start_ui();")
            self._duckdb_ui_con = con
