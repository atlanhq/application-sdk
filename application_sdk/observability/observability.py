import asyncio
import gzip
import logging
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Generic, List, TypeVar

import duckdb
import orjson

if TYPE_CHECKING:
    from obstore.store import ObjectStore

from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_NAME,
    DEPLOYMENT_OBJECT_STORE_NAME,
    ENABLE_ATLAN_UPLOAD,
    ENABLE_OBSERVABILITY_STORE_SINK,
    LOG_FILE_NAME,
    METRICS_FILE_NAME,
    TRACES_FILE_NAME,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.utils import get_observability_dir
from application_sdk.storage import delete, upload_file
from application_sdk.storage.binding import create_store_from_binding

# --- Path configuration ---
# Structure: observability/<mode>/<signal>/year=.../hour=.../file.json.gz
# SDR (ENABLE_ATLAN_UPLOAD=true):     sdr/logs/, sdr/metrics/, sdr/traces/
# Non-SDR (ENABLE_ATLAN_UPLOAD=false): non-sdr/logs/, non-sdr/metrics/, non-sdr/traces/
_OBS_MODE = "sdr" if ENABLE_ATLAN_UPLOAD else "non-sdr"

# Map of signal type → local subdirectory (e.g., sdr/logs)
LOCAL_OBS_SUBDIR_MAP = {
    "logs": f"{_OBS_MODE}/logs",
    "metrics": f"{_OBS_MODE}/metrics",
    "traces": f"{_OBS_MODE}/traces",
}

# Map of signal type → S3 remote key prefix
OBSERVABILITY_S3_PREFIX_MAP = {
    "logs": f"artifacts/apps/observability/{_OBS_MODE}/logs",
    "metrics": f"artifacts/apps/observability/{_OBS_MODE}/metrics",
    "traces": f"artifacts/apps/observability/{_OBS_MODE}/traces",
}


T = TypeVar("T")


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
    _instances: ClassVar[list[Any]] = []
    _deployment_store: ClassVar["ObjectStore | None"] = None
    _upstream_store: ClassVar["ObjectStore | None"] = None

    @classmethod
    def _get_deployment_store(cls):
        if cls._deployment_store is None:
            cls._deployment_store = create_store_from_binding(
                DEPLOYMENT_OBJECT_STORE_NAME
            )
        return cls._deployment_store

    @classmethod
    def _get_upstream_store(cls):
        if cls._upstream_store is None:
            cls._upstream_store = create_store_from_binding(UPSTREAM_OBJECT_STORE_NAME)
        return cls._upstream_store

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
        self._buffer: list[dict[str, object]] = []
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

    @classmethod
    async def flush_all(cls) -> None:
        """Flush all instances of AtlanObservability.

        This method attempts to flush all registered instances,
        logging any errors that occur during flushing.
        """
        for instance in cls._instances:
            try:
                await instance._flush_buffer(force=True)
            except Exception as e:
                logging.error(f"Error flushing instance: {e}")

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Reset global state for test isolation.

        This method should only be used in tests to allow fresh initialization
        for each test case.
        """
        cls._instances.clear()
        cls._deployment_store = None
        cls._upstream_store = None

    def _get_signal_type(self) -> str:
        """Map file_name to signal type (logs, metrics, traces).

        Returns:
            str: The signal type based on self.file_name
        """
        if self.file_name == LOG_FILE_NAME:
            return "logs"
        elif self.file_name == METRICS_FILE_NAME:
            return "metrics"
        elif self.file_name == TRACES_FILE_NAME:
            return "traces"
        return "other"

    def _get_partition_path(self, timestamp: datetime) -> str:
        """Generate local partition path based on timestamp.

        Uses signal-type-specific subdirectory (e.g., sdr-logs/, non-sdr-metrics/)
        with hour-level partitioning. The full S3 prefix is applied separately
        when computing the remote key for upload.

        Args:
            timestamp: The timestamp to generate partition path for

        Returns:
            str: The local partition path
        """
        signal_type = self._get_signal_type()
        local_subdir = LOCAL_OBS_SUBDIR_MAP.get(signal_type, f"{_OBS_MODE}/other")

        return os.path.join(
            self.data_dir,
            local_subdir,
            f"year={timestamp.year}",
            f"month={timestamp.month:02d}",
            f"day={timestamp.day:02d}",
            f"hour={timestamp.hour:02d}",
        )

    def _update_parquet_path(self):
        """Update the parquet file path based on current timestamp."""
        current_time = datetime.now()
        partition_path = self._get_partition_path(current_time)
        os.makedirs(partition_path, exist_ok=True)
        self.parquet_path = os.path.join(partition_path, "data.parquet")

    @abstractmethod
    def process_record(self, record: T) -> Dict[str, Any]:
        """Process a record into a dictionary format.

        Args:
            record: The record to process

        Returns:
            Dict[str, Any]: Dictionary representation of the record

        This is an abstract method that must be implemented by subclasses.
        """
        pass

    @abstractmethod
    def export_record(self, record: T) -> None:
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

    async def _flush_records(self, records: List[Dict[str, Any]]):
        """Flush records to json.gz files and upload to object stores.

        Args:
            records: List of records to flush

        This method:
        - Groups records by partition (year/month/day/hour)
        - Writes json.gz format (lightweight, no pandas dependency)
        - Uses centralized path based on ENABLE_ATLAN_UPLOAD
        - SDR path: artifacts/apps/observability/sdr-logs/ (MDLH reads from Atlan bucket)
        - Non-SDR path: artifacts/apps/observability/logs/ (to be deprecated)
        - Uploads to customer bucket (DEPLOYMENT_OBJECT_STORE) always
        - Uploads to Atlan bucket (UPSTREAM_OBJECT_STORE) when ENABLE_ATLAN_UPLOAD=true
        """
        if not ENABLE_OBSERVABILITY_STORE_SINK or not records:
            return
        try:
            from time import time_ns

            # Group records by partition using record's own timestamp
            partition_records: Dict[str, List[Dict[str, Any]]] = {}
            for record in records:
                record_time = datetime.fromtimestamp(record["timestamp"])
                partition_path = self._get_partition_path(record_time)

                if partition_path not in partition_records:
                    partition_records[partition_path] = []
                partition_records[partition_path].append(record)

            # Write each partition as json.gz and upload
            for partition_path, partition_data in partition_records.items():
                local_path = None
                try:
                    os.makedirs(partition_path, exist_ok=True)

                    # Get timestamp from first record for remote key partitioning
                    first_record_time = datetime.fromtimestamp(
                        partition_data[0]["timestamp"]
                    )

                    # Lexi-sortable filename
                    filename = (
                        f"{time_ns()}_{DEPLOYMENT_NAME}_{APPLICATION_NAME}.json.gz"
                    )
                    local_path = os.path.join(partition_path, filename)

                    # Write NDJSON with gzip compression
                    with gzip.open(local_path, "wb") as f:
                        for record in partition_data:
                            f.write(orjson.dumps(record) + b"\n")

                    # Compute remote key using signal-type-specific S3 prefix
                    signal_type = self._get_signal_type()
                    s3_prefix = OBSERVABILITY_S3_PREFIX_MAP.get(
                        signal_type,
                        f"artifacts/apps/observability/{_OBS_MODE}/other",
                    )
                    remote_key = os.path.join(
                        s3_prefix,
                        f"year={first_record_time.year}",
                        f"month={first_record_time.month:02d}",
                        f"day={first_record_time.day:02d}",
                        f"hour={first_record_time.hour:02d}",
                        filename,
                    )

                    # Upload to customer bucket (non-fatal if fails)
                    try:
                        await upload_file(
                            remote_key,
                            local_path,
                            store=self._get_deployment_store(),
                        )
                    except Exception as e:
                        logging.warning(
                            f"Deployment objectstore upload failed (non-fatal): {e}"
                        )

                    # Upload to Atlan bucket (independent, MDLH reads from here)
                    if ENABLE_ATLAN_UPLOAD:
                        await upload_file(
                            remote_key,
                            local_path,
                            store=self._get_upstream_store(),
                        )

                    logging.debug(
                        f"Exported {len(partition_data)} records → {remote_key}"
                    )

                except Exception as partition_error:
                    logging.error(
                        f"Error processing partition {partition_path}: {str(partition_error)}"
                    )
                finally:
                    # Always clean up local file to prevent disk leaks
                    if local_path and os.path.exists(local_path):
                        os.unlink(local_path)

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
            from application_sdk.infrastructure.context import get_infrastructure

            infra = get_infrastructure()
            state_store = infra.state_store if infra else None

            last_cleanup: str | None = None
            if state_store:
                state = await state_store.load(self._last_cleanup_key)
                last_cleanup = state.get("value") if state else None

            # If no last cleanup or it's been more than a day, perform cleanup
            if not last_cleanup or (
                datetime.now() - datetime.fromisoformat(last_cleanup)
            ) > timedelta(days=1):
                await self._cleanup_old_records()
                # Update last cleanup time
                if state_store:
                    await state_store.save(
                        self._last_cleanup_key,
                        {"value": datetime.now().isoformat()},
                    )
        except Exception as e:
            logging.error(f"Error checking cleanup status: {e}")

    async def _cleanup_old_records(self):
        """Clean up records older than retention_days.

        This method:
        - Uses the centralized SDR path
        - Walks through the partition directories
        - Removes records older than retention period
        - Updates or deletes files as needed
        - Syncs changes with object store
        """
        try:
            # Use local subdir (same as _get_partition_path)
            signal_type = self._get_signal_type()
            local_subdir = LOCAL_OBS_SUBDIR_MAP.get(signal_type, f"{_OBS_MODE}/other")
            data_dir = os.path.join(self.data_dir, local_subdir)
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
                            partition_key = os.path.relpath(day_path, self.data_dir)
                            await delete(
                                partition_key,
                                store=self._get_deployment_store(),
                            )
                            if ENABLE_ATLAN_UPLOAD:
                                await delete(
                                    partition_key,
                                    store=self._get_upstream_store(),
                                )

        except Exception as e:
            logging.error(f"Error cleaning up old records: {e}")

    def add_record(self, record: T):
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

    def __init__(self):
        """Initialize the DuckDB UI handler."""
        self.observability_dir = get_observability_dir()
        self.db_path = self.observability_dir + "/observability.db"
        self._duckdb_ui_con = None

    def _is_duckdb_ui_running(self, host="0.0.0.0", port=4213):
        """Check if DuckDB UI is already running on the default port."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            result = sock.connect_ex((host, port))
            return result == 0

    def start_ui(self):
        """Start DuckDB UI and create views for Hive partitioned json.gz files."""
        if not self._is_duckdb_ui_running():
            os.makedirs(self.observability_dir, exist_ok=True)
            con = duckdb.connect(self.db_path)

            def process_partitioned_files(directory, view_name):
                """Process Hive partitioned json.gz files and create views."""
                if not os.path.exists(directory):
                    return

                # Check if there are any json.gz files in the directory
                if not any(Path(directory).rglob("*.json.gz")):
                    return

                # Create a view that reads all json.gz files in the directory
                # using DuckDB's native Hive partitioning support
                view_query = f"""
                CREATE OR REPLACE VIEW {view_name} AS
                SELECT *
                FROM read_json_auto('{directory}/**/*.json.gz',
                                   hive_partitioning = true,
                                   hive_types = {{'year': INTEGER, 'month': INTEGER, 'day': INTEGER, 'hour': INTEGER}})
                """
                con.execute(view_query)

            # Process each signal type under the mode directory (sdr/ or non-sdr/)
            mode_dir = os.path.join(self.observability_dir, _OBS_MODE)
            for signal_type in ["logs", "metrics", "traces"]:
                data_dir = os.path.join(mode_dir, signal_type)
                if os.path.exists(data_dir):
                    process_partitioned_files(data_dir, signal_type)

            # Start DuckDB UI
            con.execute("CALL start_ui();")
            self._duckdb_ui_con = con
