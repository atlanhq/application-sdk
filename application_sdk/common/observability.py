import asyncio
import logging
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from time import time
from typing import Any, Dict, Generic, List, TypeVar

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from dapr.clients import DaprClient
from pydantic import BaseModel

from application_sdk.constants import OBJECT_STORE_NAME, STATE_STORE_NAME

T = TypeVar("T", bound=BaseModel)


class AtlanObservability(Generic[T], ABC):
    """Base class for Atlan observability."""

    _last_cleanup_key = (
        "last_cleanup_time"  # Class variable for shared cleanup tracking
    )

    def __init__(
        self,
        batch_size: int,
        flush_interval: int,
        retention_days: int,
        cleanup_enabled: bool,
        data_dir: str,
        file_name: str,
    ):
        """Initialize the observability base class.

        Args:
            batch_size: Number of records to batch before flushing
            flush_interval: Interval in seconds between forced flushes
            retention_days: Number of days to retain records
            cleanup_enabled: Whether to enable cleanup of old records
            data_dir: Directory to store data files
            file_name: Name of the data file
        """
        # Initialize instance variables
        self._buffer: List[Dict[str, Any]] = []
        self._buffer_lock = threading.Lock()
        self._last_flush_time = time()
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._retention_days = retention_days
        self._cleanup_enabled = cleanup_enabled
        self.data_dir = data_dir
        self.file_name = file_name
        self.parquet_path = os.path.join(data_dir, file_name)

        # Ensure data directory exists
        os.makedirs(data_dir, exist_ok=True)

    @abstractmethod
    def process_record(self, record: Any) -> Dict[str, Any]:
        """Process a record into a dictionary format.

        Args:
            record: The record to process

        Returns:
            Dictionary representation of the record
        """
        pass

    @abstractmethod
    def export_record(self, record: Any) -> None:
        """Export a record to external systems.

        Args:
            record: The record to export
        """
        pass

    async def _flush_buffer(self, force=False):
        """Flush the buffer."""
        with self._buffer_lock:
            if self._buffer:
                buffer_copy = self._buffer[:]
                self._buffer.clear()
            else:
                buffer_copy = None
        if buffer_copy:
            await self._flush_records(buffer_copy)

    async def _flush_records(self, records: List[Dict[str, Any]]):
        """Flush records to parquet file and object store."""
        try:
            df = pd.DataFrame(records)
            if os.path.exists(self.parquet_path):
                existing_table = pq.read_table(self.parquet_path)
                existing_df = existing_table.to_pandas()
                df = pd.concat([existing_df, df], ignore_index=True)
            table = pa.Table.from_pandas(df)
            pq.write_table(table, self.parquet_path, compression="snappy")

            # Upload to object store
            with open(self.parquet_path, "rb") as f:
                file_content = f.read()
                metadata = {
                    "key": self.file_name,
                    "blobName": self.file_name,
                    "fileName": self.file_name,
                }
                with DaprClient() as client:
                    client.invoke_binding(
                        binding_name=OBJECT_STORE_NAME,
                        operation="create",
                        data=file_content,
                        binding_metadata=metadata,
                    )

            # Clean up old records if enabled and it's been a day since last cleanup
            if self._cleanup_enabled:
                await self._check_and_cleanup()

        except Exception as e:
            logging.error(f"Error flushing records batch: {e}")

    async def _check_and_cleanup(self):
        """Check if cleanup is needed and perform it if necessary."""
        try:
            with DaprClient() as client:
                # Get last cleanup time from state store
                state = client.get_state(
                    store_name=STATE_STORE_NAME, key=self._last_cleanup_key
                )
                last_cleanup = state.data.decode() if state.data else None

                # If no last cleanup or it's been more than a day, perform cleanup
                if not last_cleanup or (
                    datetime.now() - datetime.fromisoformat(last_cleanup)
                ) > timedelta(days=1):
                    await self._cleanup_old_records()
                    # Update last cleanup time
                    client.save_state(
                        store_name=STATE_STORE_NAME,
                        key=self._last_cleanup_key,
                        value=datetime.now().isoformat(),
                    )
        except Exception as e:
            logging.error(f"Error checking cleanup status: {e}")

    async def _cleanup_old_records(self):
        """Clean up records older than retention_days."""
        try:
            if not os.path.exists(self.parquet_path):
                return

            # Read existing records
            table = pq.read_table(self.parquet_path)
            df = table.to_pandas()

            # Filter out old records
            cutoff_date = pd.Timestamp.now() - pd.Timedelta(days=self._retention_days)
            cutoff_date = cutoff_date.date()
            filtered_df = df.loc[df["timestamp"].dt.date >= cutoff_date].copy()

            # Update records file
            if len(filtered_df) > 0:
                table = pa.Table.from_pandas(filtered_df)
                pq.write_table(table, self.parquet_path, compression="snappy")

                # Update in object store
                with open(self.parquet_path, "rb") as f:
                    file_content = f.read()
                    metadata = {
                        "key": self.file_name,
                        "blobName": self.file_name,
                        "fileName": self.file_name,
                    }
                    with DaprClient() as client:
                        client.invoke_binding(
                            binding_name=OBJECT_STORE_NAME,
                            operation="create",
                            data=file_content,
                            binding_metadata=metadata,
                        )
            else:
                # If no records remain, delete the file
                os.remove(self.parquet_path)
                # Delete from object store
                with DaprClient() as client:
                    client.invoke_binding(
                        binding_name=OBJECT_STORE_NAME,
                        operation="delete",
                        data=b"",
                        binding_metadata={"key": self.file_name},
                    )

        except Exception as e:
            logging.error(f"Error cleaning up old records: {e}")

    def add_record(self, record: Any):
        """Add a record to the buffer and process it.

        Args:
            record: The record to add
        """
        try:
            # Process the record
            processed_record = self.process_record(record)

            # Add to buffer
            with self._buffer_lock:
                self._buffer.append(processed_record)
                now = time()
                if (
                    len(self._buffer) >= self._batch_size
                    or (now - self._last_flush_time) >= self._flush_interval
                ):
                    self._last_flush_time = now
                    buffer_copy = self._buffer[:]
                    self._buffer.clear()
                else:
                    buffer_copy = None

            # Flush if needed
            if buffer_copy is not None:
                asyncio.create_task(self._flush_records(buffer_copy))

            # Export the record
            self.export_record(record)

        except Exception as e:
            logging.error(f"Error adding record: {e}")
