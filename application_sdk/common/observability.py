import json
import logging
import os
import threading
from time import time
from typing import Any, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from dapr.clients import DaprClient
from pydantic import BaseModel

from application_sdk.constants import (
    LOG_BATCH_SIZE,
    LOG_CLEANUP_ENABLED,
    LOG_DIR,
    LOG_FILE_NAME,
    LOG_FLUSH_INTERVAL_SECONDS,
    LOG_RETENTION_DAYS,
    OBJECT_STORE_NAME,
    STATE_STORE_NAME,
)


class LogRecord(BaseModel):
    """Pydantic model for log records."""

    timestamp: float
    level: str
    logger_name: str
    message: str
    file: str
    line: int
    function: str
    extra: Optional[dict[str, Any]] = None


class AtlanObservability:
    """Base class for Atlan observability."""

    def __init__(self):
        """Initialize the observability base class."""
        # Initialize instance variables
        self._log_buffer = []
        self._buffer_lock = threading.Lock()
        self._last_flush_time = time()
        self._batch_size = LOG_BATCH_SIZE
        self._flush_interval = LOG_FLUSH_INTERVAL_SECONDS
        self.parquet_path = os.path.join(LOG_DIR, LOG_FILE_NAME)

        # Ensure log directory exists
        os.makedirs(LOG_DIR, exist_ok=True)

    async def parquet_sink(self, message: Any):
        try:
            log_record = LogRecord(
                timestamp=message.record["time"].timestamp(),
                level=message.record["level"].name,
                logger_name=message.record["extra"].get("logger_name", ""),
                message=message.record["message"],
                file=str(message.record["file"].path),
                line=message.record["line"],
                function=message.record["function"],
                extra={
                    k: v
                    for k, v in message.record["extra"].items()
                    if k != "logger_name"
                },
            )

            with self._buffer_lock:
                self._log_buffer.append(log_record.model_dump())
                now = time()
                if (
                    len(self._log_buffer) >= self._batch_size
                    or (now - self._last_flush_time) >= self._flush_interval
                ):
                    self._last_flush_time = now
                    buffer_copy = self._log_buffer[:]
                    self._log_buffer.clear()
                else:
                    buffer_copy = None

            if buffer_copy is not None:
                await self._flush_records(buffer_copy)
        except Exception as e:
            logging.error(f"Error buffering log: {e}")

    async def _flush_buffer(self, force=False):
        with self._buffer_lock:
            if self._log_buffer:
                buffer_copy = self._log_buffer[:]
                self._log_buffer.clear()
            else:
                buffer_copy = None
        if buffer_copy:
            await self._flush_records(buffer_copy)

    async def _flush_records(self, records):
        try:
            df = pd.DataFrame(records)
            if os.path.exists(self.parquet_path):
                existing_table = pq.read_table(self.parquet_path)
                existing_df = existing_table.to_pandas()
                df = pd.concat([existing_df, df], ignore_index=True)
            table = pa.Table.from_pandas(df)
            pq.write_table(table, self.parquet_path, compression="snappy")
            with open(self.parquet_path, "rb") as f:
                file_content = f.read()
                metadata = {
                    "key": LOG_FILE_NAME,
                    "blobName": LOG_FILE_NAME,
                    "fileName": LOG_FILE_NAME,
                }
                with DaprClient() as client:
                    client.invoke_binding(
                        binding_name=OBJECT_STORE_NAME,
                        operation="create",
                        data=file_content,
                        binding_metadata=metadata,
                    )
            # Clean up old logs after flushing
            if LOG_CLEANUP_ENABLED:
                await self._cleanup_old_logs()
        except Exception as e:
            logging.error(f"Error flushing log batch to parquet: {e}")

    async def _should_run_cleanup(self) -> bool:
        """Check if cleanup should run based on last purge date."""
        current_date = pd.Timestamp.now().date()
        try:
            with DaprClient() as client:
                state = client.get_state(
                    store_name=STATE_STORE_NAME, key="last_log_purge"
                )
                if not state.data:
                    # If no last purge date exists, set initial date and return
                    client.save_state(
                        store_name=STATE_STORE_NAME,
                        key="last_log_purge",
                        value=json.dumps({"date": current_date.isoformat()}),
                    )
                    return False
                last_purge_date = pd.Timestamp(
                    json.loads(state.data).get("date")
                ).date()
                return last_purge_date != current_date
        except Exception as e:
            logging.error(f"Error checking last purge date: {e}")
            return False

    async def _update_last_purge_date(self, current_date: pd.Timestamp):
        """Update the last purge date in state store."""
        try:
            with DaprClient() as client:
                client.save_state(
                    store_name=STATE_STORE_NAME,
                    key="last_log_purge",
                    value=json.dumps({"date": current_date.isoformat()}),
                )
        except Exception as e:
            logging.error(f"Error updating last purge date: {e}")

    async def _filter_old_logs(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter out logs older than LOG_RETENTION_DAYS.

        Args:
            df: Input DataFrame containing log records

        Returns:
            DataFrame containing only logs newer than LOG_RETENTION_DAYS
        """
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"])

        cutoff_date = pd.Timestamp.now() - pd.Timedelta(days=LOG_RETENTION_DAYS)
        cutoff_date = cutoff_date.date()

        # Ensure we return a DataFrame by using loc
        return df.loc[df["timestamp"].dt.date >= cutoff_date].copy()

    async def _update_log_file(self, df: pd.DataFrame):
        """Update the log file with filtered logs."""
        if len(df) > 0:
            table = pa.Table.from_pandas(df)
            pq.write_table(table, self.parquet_path, compression="snappy")

            # Update in object store
            with open(self.parquet_path, "rb") as f:
                file_content = f.read()
                metadata = {
                    "key": LOG_FILE_NAME,
                    "blobName": LOG_FILE_NAME,
                    "fileName": LOG_FILE_NAME,
                }
                with DaprClient() as client:
                    client.invoke_binding(
                        binding_name=OBJECT_STORE_NAME,
                        operation="create",
                        data=file_content,
                        binding_metadata=metadata,
                    )
        else:
            # If no logs remain, delete the file
            os.remove(self.parquet_path)
            # Delete from object store
            with DaprClient() as client:
                client.invoke_binding(
                    binding_name=OBJECT_STORE_NAME,
                    operation="delete",
                    data=b"",
                    binding_metadata={"key": "logs/log.parquet"},
                )

    async def _cleanup_old_logs(self):
        """Clean up logs older than LOG_RETENTION_DAYS. Runs once per day."""
        try:
            if not await self._should_run_cleanup():
                return

            if not os.path.exists(self.parquet_path):
                return

            # Read existing logs
            table = pq.read_table(self.parquet_path)
            df = table.to_pandas()

            # Filter out old logs
            filtered_df = await self._filter_old_logs(df)

            # Update log file with filtered logs
            await self._update_log_file(filtered_df)

            # Update last purge date
            await self._update_last_purge_date(pd.Timestamp.now())

        except Exception as e:
            logging.error(f"Error cleaning up old logs: {e}")


class DuckDBUI:
    """Class to handle DuckDB UI functionality."""

    def __init__(self, db_path="/tmp/logs/observability.db"):
        """Initialize the DuckDB UI handler.

        Args:
            db_path (str): Path to the DuckDB database file.
        """
        self.db_path = db_path
        self._duckdb_ui_con = None

    def _is_duckdb_ui_running(self, host="0.0.0.0", port=4213):
        """Check if DuckDB UI is already running on the default port."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            result = sock.connect_ex((host, port))
            return result == 0

    def start_ui(self):
        """Start DuckDB UI if not already running, and attach the /tmp/logs folder."""
        import os

        import duckdb

        from application_sdk.constants import LOG_DIR

        if not self._is_duckdb_ui_running():
            os.makedirs(LOG_DIR, exist_ok=True)
            con = duckdb.connect(self.db_path)
            # Attach all .parquet files in /tmp/logs as tables
            for fname in os.listdir(LOG_DIR):
                fpath = os.path.join(LOG_DIR, fname)
                if fname.endswith(".parquet"):
                    tbl = os.path.splitext(fname)[0]
                    con.execute(
                        f"CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM read_parquet('{fpath}')"
                    )
            # Start DuckDB UI using SQL command
            con.execute("CALL start_ui();")
            self._duckdb_ui_con = con  # Store the connection, do NOT close it!
