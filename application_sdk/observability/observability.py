import asyncio
import logging
import os
import signal
import sys
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

from application_sdk.constants import (
    ENABLE_OBSERVABILITY_DAPR_SINK,
    LOG_DATE_FORMAT,
    LOG_USE_DATE_BASED_FILES,
    METRICS_USE_DATE_BASED_FILES,
    OBJECT_STORE_NAME,
    OBSERVABILITY_DIR,
    STATE_STORE_NAME,
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
        """Initialize the observability base class.

        Args:
            batch_size (int): Number of records to batch before flushing
            flush_interval (int): Interval in seconds between forced flushes
            retention_days (int): Number of days to retain records
            cleanup_enabled (bool): Whether to enable cleanup of old records
            data_dir (str): Directory to store data files
            file_name (str): Name of the data file
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
        self._use_date_based_files = LOG_USE_DATE_BASED_FILES
        self._date_format = LOG_DATE_FORMAT
        self._current_date = datetime.now().strftime(self._date_format)
        self._update_parquet_path()
        self.parquet_path = os.path.join(data_dir, file_name)

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

    def _update_parquet_path(self):
        """Update the parquet file path based on current date if using date-based files.

        This method:
        - Creates appropriate subdirectories for different file types
        - Updates the parquet path based on current date if using date-based files
        - Maintains the original path if not using date-based files
        """
        if self._use_date_based_files:
            # Create appropriate subdirectory based on file type
            if self.file_name == "log.parquet":
                subdir = "logs"
            elif self.file_name == "metrics.parquet":
                subdir = "metrics"
            else:
                subdir = "other"  # Fallback for any other file types

            # Create the subdirectory
            data_subdir = os.path.join(self.data_dir, subdir)
            os.makedirs(data_subdir, exist_ok=True)

            # Use date-based filename
            date_str = datetime.now().strftime(self._date_format)
            self.parquet_path = os.path.join(data_subdir, f"{date_str}.parquet")
        else:
            self.parquet_path = os.path.join(self.data_dir, self.file_name)

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
        - Updates parquet path for date-based files
        - Writes records to parquet file
        - Uploads to object store if enabled
        - Triggers cleanup if needed
        """
        if not ENABLE_OBSERVABILITY_DAPR_SINK:
            return
        try:
            # Update parquet path in case date has changed
            self._update_parquet_path()

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
                    "key": os.path.basename(self.parquet_path),
                    "blobName": os.path.basename(self.parquet_path),
                    "fileName": os.path.basename(self.parquet_path),
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
        - Handles both single file and date-based file cases
        - Removes records older than retention period
        - Updates or deletes files as needed
        - Syncs changes with object store
        """
        try:
            if not self._use_date_based_files:
                # Handle single file case
                if not os.path.exists(self.parquet_path):
                    return

                # Read existing records
                table = pq.read_table(self.parquet_path)
                df = table.to_pandas()

                # Convert timestamp to datetime
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")

                # Filter out old records
                cutoff_date = pd.Timestamp.now() - pd.Timedelta(
                    days=self._retention_days
                )
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
                            "key": os.path.basename(self.parquet_path),
                            "blobName": os.path.basename(self.parquet_path),
                            "fileName": os.path.basename(self.parquet_path),
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
                            binding_metadata={
                                "key": os.path.basename(self.parquet_path)
                            },
                        )
            else:
                # Handle date-based files
                # Determine the appropriate subdirectory
                if self.file_name == "log.parquet":
                    subdir = "logs"
                elif self.file_name == "metrics.parquet":
                    subdir = "metrics"
                else:
                    subdir = "other"

                data_subdir = os.path.join(self.data_dir, subdir)
                if not os.path.exists(data_subdir):
                    return

                cutoff_date = datetime.now() - timedelta(days=self._retention_days)

                # List all parquet files in the subdirectory
                for filename in os.listdir(data_subdir):
                    if filename.endswith(".parquet"):
                        file_date_str = filename.replace(".parquet", "")
                        try:
                            file_date = datetime.strptime(
                                file_date_str, self._date_format
                            )
                            if file_date.date() < cutoff_date.date():
                                # Delete old file
                                file_path = os.path.join(data_subdir, filename)
                                os.remove(file_path)
                                # Delete from object store
                                with DaprClient() as client:
                                    client.invoke_binding(
                                        binding_name=OBJECT_STORE_NAME,
                                        operation="delete",
                                        data=b"",
                                        binding_metadata={"key": filename},
                                    )
                        except ValueError:
                            # Skip files that don't match the date format
                            continue

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
    """Class to handle DuckDB UI functionality.

    This class provides functionality to start and manage the DuckDB UI,
    including automatic view creation for parquet files.

    Attributes:
        db_path (str): Path to the DuckDB database file
        _duckdb_ui_con: DuckDB connection object
    """

    def __init__(self, db_path="/tmp/observability/observability.db"):
        """Initialize the DuckDB UI handler.

        Args:
            db_path (str): Path to the DuckDB database file
        """
        self.db_path = db_path
        self._duckdb_ui_con = None

    def _is_duckdb_ui_running(self, host="0.0.0.0", port=4213):
        """Check if DuckDB UI is already running on the default port.

        Args:
            host (str): Host to check
            port (int): Port to check

        Returns:
            bool: True if DuckDB UI is running, False otherwise
        """
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            result = sock.connect_ex((host, port))
            return result == 0

    def start_ui(self, db_path=OBSERVABILITY_DIR):
        """Start DuckDB UI if not already running, and attach the observability folder.

        Args:
            db_path (str): Path to the observability directory

        This method:
        - Creates necessary directories
        - Connects to DuckDB
        - Creates views for parquet files
        - Starts the DuckDB UI
        """
        import os

        import duckdb

        if not self._is_duckdb_ui_running():
            os.makedirs(OBSERVABILITY_DIR, exist_ok=True)
            con = duckdb.connect(self.db_path)

            # Function to process parquet files and create views
            def process_parquet_files(directory, prefix=""):
                for fname in os.listdir(directory):
                    fpath = os.path.join(directory, fname)
                    if fname.endswith(".parquet"):
                        # For date-based files, use the date as part of the table name
                        # Replace hyphens with underscores to make it DuckDB compatible
                        base_name = os.path.splitext(fname)[0].replace("-", "_")
                        if prefix:
                            tbl = f"{prefix}_{base_name}"
                        else:
                            tbl = base_name
                        con.execute(
                            f"CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM read_parquet('{fpath}')"
                        )

            # Process files in the main directory (non-date-based case)
            process_parquet_files(OBSERVABILITY_DIR)

            # Process date-based files if enabled
            if LOG_USE_DATE_BASED_FILES:
                logs_dir = os.path.join(OBSERVABILITY_DIR, "logs")
                if os.path.exists(logs_dir):
                    process_parquet_files(logs_dir, "logs")

            if METRICS_USE_DATE_BASED_FILES:
                metrics_dir = os.path.join(OBSERVABILITY_DIR, "metrics")
                if os.path.exists(metrics_dir):
                    process_parquet_files(metrics_dir, "metrics")

            # Start DuckDB UI using SQL command
            con.execute("CALL start_ui();")
            self._duckdb_ui_con = con  # Store the connection, do NOT close it!
