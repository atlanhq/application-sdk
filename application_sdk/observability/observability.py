import asyncio
import logging
import os
import signal
import sys
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from time import time
from typing import Any, Dict, Generic, List, TypeVar

import duckdb
import pandas as pd
from dapr.clients import DaprClient
from pydantic import BaseModel

from application_sdk.constants import (
    ENABLE_OBSERVABILITY_DAPR_SINK,
    LOG_FILE_NAME,
    METRICS_FILE_NAME,
    OBJECT_STORE_NAME,
    OBSERVABILITY_DIR,
    STATE_STORE_NAME,
    TRACES_FILE_NAME,
)


class LogRecord(BaseModel):
    """A Pydantic model representing a log record in the system.

    This model defines the structure for log data with fields for timestamp,
    level, logger name, message, and source location information.

    Attributes:
        timestamp (float): Unix timestamp when the log was recorded
        level (str): Log level (DEBUG, INFO, WARNING, ERROR, etc.)
        logger_name (str): Name of the logger that created the record
        message (str): The actual log message
        file (str): Source file where the log was created
        line (int): Line number in the source file
        function (str): Function name where the log was created
        extra (Dict[str, Any]): Additional context data for the log
    """

    timestamp: float
    level: str
    logger_name: str
    message: str
    file: str
    line: int
    function: str
    extra: Dict[str, Any]


T = TypeVar("T", bound=BaseModel)


class AtlanObservability(Generic[T], ABC):
    """Base class for Atlan observability functionality.

    This abstract base class provides core functionality for observability features
    including buffering, flushing, and cleanup of observability data.

    Features:
    - Buffered record storage
    - Periodic flushing to storage
    - Data retention management
    - Error handling and signal management
    - Parquet file storage
    - Dapr object store integration

    Attributes:
        _last_cleanup_key: Key for tracking last cleanup time
        _instances: List of all active instances
    """

    _last_cleanup_key = "last_cleanup_time"
    _instances = []

    def __init__(
        self,
        batch_size: int,
        flush_interval: int,
        retention_days: int,
        cleanup_enabled: bool,
        data_dir: str,
        file_name: str,
    ):
        """Initialize the observability base class."""
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
        self._update_parquet_path()

        # Ensure data directory exists
        os.makedirs(data_dir, exist_ok=True)

        # Register this instance
        AtlanObservability._instances.append(self)

        # Set up signal handlers and exception hook if not already set
        if not hasattr(AtlanObservability, "_handlers_setup"):
            self._setup_error_handlers()
            AtlanObservability._handlers_setup = True

    def _setup_error_handlers(self):
        """Set up signal handlers and exception hook.

        This method configures:
        - Signal handlers for SIGTERM and SIGINT
        - Global exception hook for unhandled exceptions
        Both handlers ensure data is flushed before termination.
        """
        # Set up signal handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._signal_handler)

        # Set up exception hook
        sys.excepthook = self._exception_hook

    def _signal_handler(self, signum, frame):
        """Handle system signals by flushing logs.

        Args:
            signum: Signal number
            frame: Current stack frame

        This method:
        - Logs the received signal
        - Attempts to flush all instances
        - Exits the process
        """
        logging.warning(f"Received signal {signum}, flushing logs...")
        try:
            # Try to get the current event loop
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If we're in an async context, create a task
                    asyncio.create_task(self._flush_all_instances())
                else:
                    # If we have a loop but it's not running, run the flush
                    loop.run_until_complete(self._flush_all_instances())
            except RuntimeError:
                # If no event loop exists, create a new one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._flush_all_instances())
                finally:
                    loop.close()
        except Exception as e:
            logging.error(f"Error during signal handler flush: {e}")
        sys.exit(0)

    def _exception_hook(self, exc_type, exc_value, exc_traceback):
        """Handle unhandled exceptions by flushing logs.

        Args:
            exc_type: Type of the exception
            exc_value: Exception value
            exc_traceback: Exception traceback

        This method:
        - Logs the unhandled exception
        - Attempts to flush all instances
        - Calls the original exception hook
        """
        logging.error(
            "Unhandled exception occurred, flushing logs...",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        try:
            # Try to get the current event loop
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If we're in an async context, create a task
                    asyncio.create_task(self._flush_all_instances())
                else:
                    # If we have a loop but it's not running, run the flush
                    loop.run_until_complete(self._flush_all_instances())
            except RuntimeError:
                # If no event loop exists, create a new one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._flush_all_instances())
                finally:
                    loop.close()
        except Exception as e:
            logging.error(f"Error during exception hook flush: {e}")
        # Call the original exception hook
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    @classmethod
    async def _flush_all_instances(cls):
        """Flush all instances of AtlanObservability.

        This method attempts to flush all registered instances,
        logging any errors that occur during flushing.
        """
        for instance in cls._instances:
            try:
                await instance._flush_buffer(force=True)
            except Exception as e:
                logging.error(f"Error flushing instance: {e}")

    def _get_partition_path(self, timestamp: datetime) -> str:
        """Generate Hive partition path based on timestamp.

        Args:
            timestamp: The timestamp to generate partition path for

        Returns:
            str: The partition path
        """
        # Determine the base directory based on file type
        if self.file_name == LOG_FILE_NAME:
            base_dir = "logs"
        elif self.file_name == METRICS_FILE_NAME:
            base_dir = "metrics"
        elif self.file_name == TRACES_FILE_NAME:
            base_dir = "traces"
        else:
            base_dir = "other"

        # Create partition path components
        partition_path = os.path.join(
            self.data_dir,
            base_dir,
            f"year={timestamp.year}",
            f"month={timestamp.month:02d}",
            f"day={timestamp.day:02d}",
        )

        return partition_path

    def _update_parquet_path(self):
        """Update the parquet file path based on current timestamp."""
        current_time = datetime.now()
        partition_path = self._get_partition_path(current_time)
        os.makedirs(partition_path, exist_ok=True)
        self.parquet_path = os.path.join(partition_path, "data.parquet")

    @abstractmethod
    def process_record(self, record: Any) -> Dict[str, Any]:
        """Process a record into a dictionary format.

        Args:
            record: The record to process

        Returns:
            Dict[str, Any]: Dictionary representation of the record

        This is an abstract method that must be implemented by subclasses.
        """
        pass

    @abstractmethod
    def export_record(self, record: Any) -> None:
        """Export a record to external systems.

        Args:
            record: The record to export

        This is an abstract method that must be implemented by subclasses.
        """
        pass

    async def _periodic_flush(self):
        """Periodically flush the buffer to storage.

        This coroutine:
        - Performs initial flush
        - Runs periodic flushes at configured intervals
        - Handles task cancellation gracefully
        - Ensures final flush on cancellation
        """
        try:
            # Initial flush
            await self._flush_buffer(force=True)

            while True:
                await asyncio.sleep(self._flush_interval)
                await self._flush_buffer(force=True)
        except asyncio.CancelledError:
            # Handle task cancellation gracefully
            await self._flush_buffer(force=True)
        except Exception as e:
            logging.error(f"Error in periodic flush: {e}")

    async def _flush_buffer(self, force=False):
        """Flush the buffer to storage.

        Args:
            force (bool): Whether to force flush regardless of buffer size

        This method:
        - Safely copies and clears the buffer
        - Flushes records if buffer is not empty
        """
        with self._buffer_lock:
            if self._buffer:
                buffer_copy = self._buffer[:]
                self._buffer.clear()
            else:
                buffer_copy = None
        if buffer_copy:
            await self._flush_records(buffer_copy)

    async def parquet_sink(self, message: Any):
        """Process and buffer a log message for parquet storage.

        Args:
            message: The log message to process

        This method:
        - Creates a LogRecord from the message
        - Adds it to the buffer
        - Triggers flush if buffer size or time threshold is reached
        """
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
                self._buffer.append(log_record.model_dump())
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

            if buffer_copy is not None:
                await self._flush_records(buffer_copy)
        except Exception as e:
            logging.error(f"Error buffering log: {e}")

    async def _flush_records(self, records: List[Dict[str, Any]]):
        """Flush records to parquet file and object store.

        Args:
            records: List of records to flush

        This method:
        - Groups records by partition
        - Writes records to each partition
        - Uploads to object store if enabled
        - Cleans up old records if enabled
        """
        if not ENABLE_OBSERVABILITY_DAPR_SINK:
            return
        try:
            # Group records by partition
            partition_records = {}
            for record in records:
                # Convert timestamp to datetime
                record_time = datetime.fromtimestamp(record["timestamp"])
                partition_path = self._get_partition_path(record_time)

                if partition_path not in partition_records:
                    partition_records[partition_path] = []
                partition_records[partition_path].append(record)

            # Write records to each partition
            for partition_path, partition_data in partition_records.items():
                os.makedirs(partition_path, exist_ok=True)
                # Use a consistent file name for each partition
                parquet_path = os.path.join(partition_path, "data.parquet")

                # Read existing data if any
                existing_df = None
                if os.path.exists(parquet_path):
                    try:
                        # Read the entire parquet file without excluding any columns
                        existing_df = pd.read_parquet(parquet_path)
                    except Exception as e:
                        logging.error(f"Error reading existing parquet file: {e}")
                        # If there's an error reading the existing file, we'll overwrite it
                        existing_df = None

                # Create new dataframe from current records
                new_df = pd.DataFrame(partition_data)

                # Extract partition values from path
                partition_parts = os.path.basename(os.path.dirname(parquet_path)).split(
                    os.sep
                )
                for part in partition_parts:
                    if part.startswith("year="):
                        new_df["year"] = int(part.split("=")[1])
                    elif part.startswith("month="):
                        new_df["month"] = int(part.split("=")[1])
                    elif part.startswith("day="):
                        new_df["day"] = int(part.split("=")[1])

                # Merge with existing data if any
                if existing_df is not None:
                    df = pd.concat([existing_df, new_df], ignore_index=True)
                else:
                    df = new_df

                # Sort by timestamp to maintain order
                df = df.sort_values("timestamp")

                # Write to parquet file
                df.to_parquet(
                    parquet_path,
                    compression="snappy",
                    index=False,
                )

                # Upload to object store
                with open(parquet_path, "rb") as f:
                    file_content = f.read()
                    metadata = {
                        "key": os.path.relpath(parquet_path, self.data_dir),
                        "blobName": os.path.relpath(parquet_path, self.data_dir),
                        "fileName": os.path.basename(parquet_path),
                    }
                    with DaprClient() as client:
                        client.invoke_binding(
                            binding_name=OBJECT_STORE_NAME,
                            operation="create",
                            data=file_content,
                            binding_metadata=metadata,
                        )

            # Clean up old records if enabled
            if self._cleanup_enabled:
                await self._check_and_cleanup()

        except Exception as e:
            logging.error(f"Error flushing records batch: {e}")

    async def _check_and_cleanup(self):
        """Check if cleanup is needed and perform it if necessary.

        This method:
        - Checks last cleanup time from state store
        - Performs cleanup if more than a day has passed
        - Updates last cleanup time after successful cleanup
        """
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
        """Clean up records older than retention_days.

        This method:
        - Determines the base directory
        - Walks through the partition directories
        - Removes records older than retention period
        - Updates or deletes files as needed
        - Syncs changes with object store
        """
        try:
            # Determine the base directory
            if self.file_name == LOG_FILE_NAME:
                base_dir = "logs"
            elif self.file_name == METRICS_FILE_NAME:
                base_dir = "metrics"
            elif self.file_name == TRACES_FILE_NAME:
                base_dir = "traces"
            else:
                base_dir = "other"

            data_dir = os.path.join(self.data_dir, base_dir)
            if not os.path.exists(data_dir):
                return

            cutoff_date = datetime.now() - timedelta(days=self._retention_days)

            # Walk through the partition directories
            for year_dir in os.listdir(data_dir):
                year_path = os.path.join(data_dir, year_dir)
                if not os.path.isdir(year_path) or not year_dir.startswith("year="):
                    continue

                year = int(year_dir.split("=")[1])
                if year < cutoff_date.year:
                    # Delete entire year directory
                    import shutil

                    shutil.rmtree(year_path)
                    continue

                for month_dir in os.listdir(year_path):
                    month_path = os.path.join(year_path, month_dir)
                    if not os.path.isdir(month_path) or not month_dir.startswith(
                        "month="
                    ):
                        continue

                    month = int(month_dir.split("=")[1])
                    if year == cutoff_date.year and month < cutoff_date.month:
                        shutil.rmtree(month_path)
                        continue

                    for day_dir in os.listdir(month_path):
                        day_path = os.path.join(month_path, day_dir)
                        if not os.path.isdir(day_path) or not day_dir.startswith(
                            "day="
                        ):
                            continue

                        day = int(day_dir.split("=")[1])
                        partition_date = datetime(year, month, day)

                        if partition_date.date() < cutoff_date.date():
                            # Delete entire partition directory
                            shutil.rmtree(day_path)

                            # Delete from object store
                            with DaprClient() as client:
                                client.invoke_binding(
                                    binding_name=OBJECT_STORE_NAME,
                                    operation="delete",
                                    data=b"",
                                    binding_metadata={
                                        "key": os.path.relpath(day_path, self.data_dir)
                                    },
                                )

        except Exception as e:
            logging.error(f"Error cleaning up old records: {e}")

    def add_record(self, record: Any):
        """Add a record to the buffer and process it.

        Args:
            record: The record to add

        This method:
        - Processes the record
        - Adds it to the buffer
        - Triggers flush if needed
        - Exports the record
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


class DuckDBUI:
    """Class to handle DuckDB UI functionality."""

    def __init__(self, db_path="/tmp/observability/observability.db"):
        """Initialize the DuckDB UI handler."""
        self.db_path = db_path
        self._duckdb_ui_con = None

    def _is_duckdb_ui_running(self, host="0.0.0.0", port=4213):
        """Check if DuckDB UI is already running on the default port."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            result = sock.connect_ex((host, port))
            return result == 0

    def start_ui(self, db_path=OBSERVABILITY_DIR):
        """Start DuckDB UI and create views for Hive partitioned parquet files."""
        if not self._is_duckdb_ui_running():
            os.makedirs(OBSERVABILITY_DIR, exist_ok=True)
            con = duckdb.connect(self.db_path)

            def process_partitioned_files(directory, prefix=""):
                """Process Hive partitioned parquet files and create views."""
                # Skip if directory doesn't exist
                if not os.path.exists(directory):
                    return

                # Check if there are any parquet files in the directory
                if not any(Path(directory).rglob("*.parquet")):
                    return

                # Create view name based on data type
                view_name = prefix if prefix else "data"

                # Create a view that reads all parquet files in the directory
                # using DuckDB's native Hive partitioning support
                view_query = f"""
                CREATE OR REPLACE VIEW {view_name} AS
                SELECT *
                FROM read_parquet('{directory}/**/*.parquet',
                                hive_partitioning = true,
                                hive_types = {{'year': INTEGER, 'month': INTEGER, 'day': INTEGER}})
                """
                con.execute(view_query)

            # Process each type of data
            for data_type in ["logs", "metrics", "traces"]:
                data_dir = os.path.join(OBSERVABILITY_DIR, data_type)
                if os.path.exists(data_dir):
                    process_partitioned_files(data_dir, data_type)

            # Start DuckDB UI
            con.execute("CALL start_ui();")
            self._duckdb_ui_con = con
