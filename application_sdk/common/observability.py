import json
import logging
import os
import threading
from time import time
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from dapr.clients import DaprClient

from application_sdk.constants import (
    LOG_BATCH_SIZE,
    LOG_CLEANUP_ENABLED,
    LOG_DIR,
    LOG_FILE_NAME,
    LOG_FLUSH_INTERVAL,
    LOG_RETENTION_DAYS,
    OBJECT_STORE_NAME,
    STATE_STORE_NAME,
)


class AtlanObservability:
    """Base class for Atlan observability."""

    def __init__(self):
        """Initialize the observability base class."""
        # Initialize instance variables
        self._log_buffer = []
        self._buffer_lock = threading.Lock()
        self._last_flush_time = time()
        self._batch_size = LOG_BATCH_SIZE
        self._flush_interval = LOG_FLUSH_INTERVAL
        self.parquet_path = os.path.join(LOG_DIR, LOG_FILE_NAME)

        # Ensure log directory exists
        os.makedirs(LOG_DIR, exist_ok=True)

    async def parquet_sink(self, message: Any):
        try:
            log_record = {
                "timestamp": message.record["time"].timestamp(),
                "level": message.record["level"].name,
                "logger_name": message.record["extra"].get("logger_name", ""),
                "message": message.record["message"],
                "file": str(message.record["file"].path),
                "line": message.record["line"],
                "function": message.record["function"],
            }
            for key, value in message.record["extra"].items():
                if key not in log_record:
                    log_record[key] = value

            with self._buffer_lock:
                self._log_buffer.append(log_record)
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
                filename = "logs/log.parquet"
                metadata = {
                    "key": filename,
                    "blobName": filename,
                    "fileName": filename,
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

    async def _cleanup_old_logs(self):
        """Clean up logs older than LOG_RETENTION_DAYS. Runs once per day."""
        try:
            current_date = pd.Timestamp.now().date()

            # Check if we've already run cleanup today
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
                        return
                    last_purge_date = pd.Timestamp(
                        json.loads(state.data).get("date")
                    ).date()
                    if last_purge_date == current_date:
                        return
            except Exception as e:
                logging.error(f"Error checking last purge date: {e}")
                return

            if not os.path.exists(self.parquet_path):
                return

            # Read existing logs
            table = pq.read_table(self.parquet_path)
            df = table.to_pandas()

            # Convert timestamp to datetime if it's not already
            if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                df["timestamp"] = pd.to_datetime(df["timestamp"])

            # Calculate cutoff date (LOG_RETENTION_DAYS ago from today)
            cutoff_date = pd.Timestamp.now() - pd.Timedelta(days=LOG_RETENTION_DAYS)
            cutoff_date = cutoff_date.date()

            # Filter out logs older than LOG_RETENTION_DAYS
            df = df[df["timestamp"].dt.date >= cutoff_date]

            if len(df) > 0:
                # Write back filtered logs
                table = pa.Table.from_pandas(df)
                pq.write_table(table, self.parquet_path, compression="snappy")

                # Update in object store
                with open(self.parquet_path, "rb") as f:
                    file_content = f.read()
                    filename = "logs/log.parquet"
                    metadata = {
                        "key": filename,
                        "blobName": filename,
                        "fileName": filename,
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

            # Update last purge date in state store
            with DaprClient() as client:
                client.save_state(
                    store_name=STATE_STORE_NAME,
                    key="last_log_purge",
                    value=json.dumps({"date": current_date.isoformat()}),
                )

        except Exception as e:
            logging.error(f"Error cleaning up old logs: {e}")
