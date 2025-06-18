import asyncio
import logging
import sys
import threading
from contextvars import ContextVar
from time import time_ns
from typing import Any, Dict, Optional, Tuple

from loguru import logger
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LogRecord
from opentelemetry.sdk._logs._internal.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace.span import TraceFlags
from pydantic import BaseModel, Field

from application_sdk.constants import (
    ENABLE_OBSERVABILITY_DAPR_SINK,
    ENABLE_OTLP_LOGS,
    LOG_BATCH_SIZE,
    LOG_CLEANUP_ENABLED,
    LOG_FILE_NAME,
    LOG_FLUSH_INTERVAL_SECONDS,
    LOG_LEVEL,
    LOG_RETENTION_DAYS,
    OBSERVABILITY_DIR,
    OTEL_BATCH_DELAY_MS,
    OTEL_BATCH_SIZE,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_TIMEOUT_SECONDS,
    OTEL_QUEUE_SIZE,
    OTEL_RESOURCE_ATTRIBUTES,
    OTEL_WF_NODE_NAME,
    SERVICE_NAME,
    SERVICE_VERSION,
)
from application_sdk.observability.observability import AtlanObservability
from application_sdk.observability.utils import get_workflow_context


class LogExtraModel(BaseModel):
    """Pydantic model for log extra fields."""

    client_host: Optional[str] = None
    duration_ms: Optional[int] = None
    method: Optional[str] = None
    path: Optional[str] = None
    request_id: Optional[str] = None
    status_code: Optional[int] = None
    url: Optional[str] = None
    # Workflow context
    workflow_id: Optional[str] = None
    run_id: Optional[str] = None
    workflow_type: Optional[str] = None
    namespace: Optional[str] = None
    task_queue: Optional[str] = None
    attempt: Optional[int] = None
    # Activity context
    activity_id: Optional[str] = None
    activity_type: Optional[str] = None
    schedule_to_close_timeout: Optional[str] = None
    start_to_close_timeout: Optional[str] = None
    schedule_to_start_timeout: Optional[str] = None
    heartbeat_timeout: Optional[str] = None
    # Other fields
    log_type: Optional[str] = None

    class Config:
        """Pydantic model configuration for LogExtraModel.

        Provides custom parsing logic for converting dictionary values to appropriate types.
        Handles type conversion for various fields like integers, strings, and other data types.
        """

        @classmethod
        def parse_obj(cls, obj):
            if isinstance(obj, dict):
                # Define type mappings for each field
                type_mappings = {
                    # Integer fields
                    "attempt": int,
                    "duration_ms": int,
                    "status_code": int,
                    # String fields
                    "client_host": str,
                    "method": str,
                    "path": str,
                    "request_id": str,
                    "url": str,
                    "workflow_id": str,
                    "run_id": str,
                    "workflow_type": str,
                    "namespace": str,
                    "task_queue": str,
                    "activity_id": str,
                    "activity_type": str,
                    "schedule_to_close_timeout": str,
                    "start_to_close_timeout": str,
                    "schedule_to_start_timeout": str,
                    "heartbeat_timeout": str,
                    "log_type": str,
                }

                # Process each field with its type conversion
                for field, type_func in type_mappings.items():
                    if field in obj and obj[field] is not None:
                        try:
                            obj[field] = type_func(obj[field])
                        except (ValueError, TypeError):
                            obj[field] = None

            return super().parse_obj(obj)


class LogRecordModel(BaseModel):
    """Pydantic model for log records."""

    timestamp: float
    level: str
    logger_name: str
    message: str
    file: str
    line: int
    function: str
    extra: LogExtraModel = Field(default_factory=LogExtraModel)

    @classmethod
    def from_loguru_message(cls, message: Any) -> "LogRecordModel":
        """Create a LogRecordModel from a loguru message.

        Args:
            message: Loguru message object

        Returns:
            LogRecordModel: Parsed log record model
        """
        # Create LogExtraModel for structured extra fields
        extra = LogExtraModel()
        for k, v in message.record["extra"].items():
            if k != "logger_name" and hasattr(extra, k):
                setattr(extra, k, v)

        return cls(
            timestamp=message.record["time"].timestamp(),
            level=message.record["level"].name,
            logger_name=message.record["extra"].get("logger_name", ""),
            message=message.record["message"],
            file=str(message.record["file"].path),
            line=message.record["line"],
            function=message.record["function"],
            extra=extra,
        )

    class Config:
        """Pydantic model configuration for LogRecordModel."""

        arbitrary_types_allowed = True


# Create a context variable for request_id
request_context: ContextVar[Dict[str, Any]] = ContextVar("request_context", default={})


# Add a Loguru handler for the Python logging system
class InterceptHandler(logging.Handler):
    """A custom logging handler that intercepts Python's standard logging and forwards it to Loguru.

    This handler ensures that all Python standard library logging is properly formatted and
    forwarded to Loguru's logging system, maintaining consistent logging across the application.
    """

    def emit(self, record):
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1

        # Add logger_name to extra to prevent KeyError
        logger_extras = {"logger_name": record.name}

        logger.opt(depth=depth, exception=record.exc_info).bind(**logger_extras).log(
            level, record.getMessage()
        )


logging.basicConfig(
    level=logging.getLevelNamesMapping()[LOG_LEVEL], handlers=[InterceptHandler()]
)

# Add these constants
SEVERITY_MAPPING = {
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "WARNING": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
    "CRITICAL": SeverityNumber.FATAL,
    "ACTIVITY": SeverityNumber.INFO,  # Using INFO severity for activity level
    "METRIC": SeverityNumber.INFO,  # Using INFO severity for metric level
    "TRACING": SeverityNumber.INFO,  # Using INFO severity for tracing level
}


class AtlanLoggerAdapter(AtlanObservability[LogRecordModel]):
    """A custom logger adapter for Atlan that extends AtlanObservability.

    This adapter provides enhanced logging capabilities including:
    - Structured logging with context
    - OpenTelemetry integration
    - Parquet file logging
    - Custom log levels for activities, metrics, and tracing
    - Temporal workflow and activity context integration
    """

    _flush_task_started = False
    _flush_task = None

    def __init__(self, logger_name: str) -> None:
        """Initialize the AtlanLoggerAdapter with enhanced configuration.

        Args:
            logger_name (str): Name of the logger instance.

        This initialization:
        - Sets up Loguru with custom formatting
        - Configures custom log levels (ACTIVITY, METRIC, TRACING)
        - Sets up OTLP logging if enabled
        - Initializes parquet logging if Dapr sink is enabled
        - Starts periodic flush task for log buffering
        """
        super().__init__(
            batch_size=LOG_BATCH_SIZE,
            flush_interval=LOG_FLUSH_INTERVAL_SECONDS,
            retention_days=LOG_RETENTION_DAYS,
            cleanup_enabled=LOG_CLEANUP_ENABLED,
            data_dir=OBSERVABILITY_DIR,
            file_name=LOG_FILE_NAME,
        )
        self.logger_name = logger_name
        # Bind the logger name when creating the logger instance
        self.logger = logger
        logger.remove()

        # Register custom log level for activity
        if "ACTIVITY" not in logger._core.levels:
            logger.level("ACTIVITY", no=20, color="<cyan>", icon="🔵")

        # Register custom log level for metrics
        if "METRIC" not in logger._core.levels:
            logger.level("METRIC", no=20, color="<yellow>", icon="📊")

        # Register custom log level for tracing
        if "TRACING" not in logger._core.levels:
            logger.level("TRACING", no=20, color="<magenta>", icon="🔍")

        # Update format string to use the bound logger_name
        atlan_format_str = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> <blue>[{level}]</blue> <cyan>{extra[logger_name]}</cyan> - <level>{message}</level>"
        self.logger.add(
            sys.stderr, format=atlan_format_str, level=LOG_LEVEL, colorize=True
        )

        # Add sink for parquet logging only if Dapr sink is enabled
        if ENABLE_OBSERVABILITY_DAPR_SINK:
            self.logger.add(self.parquet_sink, level=LOG_LEVEL)
            # Start flush task only if Dapr sink is enabled
            if not AtlanLoggerAdapter._flush_task_started:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        AtlanLoggerAdapter._flush_task = loop.create_task(
                            self._periodic_flush()
                        )
                    else:
                        threading.Thread(
                            target=self._start_asyncio_flush, daemon=True
                        ).start()
                    AtlanLoggerAdapter._flush_task_started = True
                except Exception as e:
                    logging.error(f"Failed to start flush task: {e}")

        # OTLP handler setup
        if ENABLE_OTLP_LOGS:
            try:
                # Get workflow node name for Argo environment
                workflow_node_name = OTEL_WF_NODE_NAME

                # First try to get attributes from OTEL_RESOURCE_ATTRIBUTES
                resource_attributes = {}
                if OTEL_RESOURCE_ATTRIBUTES:
                    resource_attributes = self._parse_otel_resource_attributes(
                        OTEL_RESOURCE_ATTRIBUTES
                    )

                # Only add default service attributes if they're not already present
                if "service.name" not in resource_attributes:
                    resource_attributes["service.name"] = SERVICE_NAME
                if "service.version" not in resource_attributes:
                    resource_attributes["service.version"] = SERVICE_VERSION

                # Add workflow node name if running in Argo
                if workflow_node_name:
                    resource_attributes["k8s.workflow.node.name"] = workflow_node_name

                self.logger_provider = LoggerProvider(
                    resource=Resource.create(resource_attributes)
                )

                exporter = OTLPLogExporter(
                    endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
                    timeout=OTEL_EXPORTER_TIMEOUT_SECONDS,
                )

                batch_processor = BatchLogRecordProcessor(
                    exporter,
                    schedule_delay_millis=OTEL_BATCH_DELAY_MS,
                    max_export_batch_size=OTEL_BATCH_SIZE,
                    max_queue_size=OTEL_QUEUE_SIZE,
                )

                self.logger_provider.add_log_record_processor(batch_processor)

                # Add OTLP sink
                self.logger.add(self.otlp_sink, level=LOG_LEVEL)

            except Exception as e:
                logging.error(f"Failed to setup OTLP logging: {str(e)}")

    def _parse_otel_resource_attributes(self, env_var: str) -> dict[str, str]:
        """Parse OpenTelemetry resource attributes from environment variable.

        Args:
            env_var (str): Comma-separated string of key-value pairs.

        Returns:
            dict[str, str]: Dictionary of parsed resource attributes.

        Example:
            Input: "service.name=myapp,service.version=1.0"
            Output: {"service.name": "myapp", "service.version": "1.0"}
        """
        try:
            # Check if the environment variable is not empty
            if env_var:
                # Split the string by commas to get individual key-value pairs
                attributes = env_var.split(",")
                # Create a dictionary from the key-value pairs
                return {
                    item.split("=")[0].strip(): item.split("=")[1].strip()
                    for item in attributes
                    if "=" in item
                }
        except Exception as e:
            logging.error(f"Failed to parse OTLP resource attributes: {str(e)}")
        return {}

    def process_record(self, record: Any) -> Dict[str, Any]:
        """Process a log record into a standardized dictionary format.

        Args:
            record (Any): Input log record, can be LogRecordModel, loguru message, or dict.

        Returns:
            Dict[str, Any]: Standardized dictionary representation of the log record.

        Raises:
            ValueError: If the record format is not supported.
        """
        if isinstance(record, LogRecordModel):
            return record.model_dump()

        # Handle loguru message format
        if hasattr(record, "record"):
            extra = LogExtraModel()
            for k, v in record.record["extra"].items():
                if k != "logger_name" and hasattr(extra, k):
                    setattr(extra, k, v)

            return LogRecordModel(
                timestamp=record.record["time"].timestamp(),
                level=record.record["level"].name,
                logger_name=record.record["extra"].get("logger_name", ""),
                message=record.record["message"],
                file=str(record.record["file"].path),
                line=record.record["line"],
                function=record.record["function"],
                extra=extra,
            ).model_dump()

        # Handle raw dictionary format
        if isinstance(record, dict):
            extra = LogExtraModel()
            for k, v in record.get("extra", {}).items():
                if hasattr(extra, k):
                    setattr(extra, k, v)
            record["extra"] = extra
            return LogRecordModel(**record).model_dump()

        raise ValueError(f"Unsupported record format: {type(record)}")

    def export_record(self, record: Any) -> None:
        """Export a log record to external systems.

        Args:
            record (Any): Log record to export.

        This method:
        - Converts the record to LogRecordModel if needed
        - Sends the record to OpenTelemetry if enabled
        """
        if not isinstance(record, LogRecordModel):
            record = LogRecordModel(**self.process_record(record))

        # Send to OpenTelemetry if enabled
        if ENABLE_OTLP_LOGS:
            self._send_to_otel(record)

    def _create_log_record(self, record: dict) -> LogRecord:
        """Create an OpenTelemetry LogRecord from a dictionary.

        Args:
            record (dict): Dictionary containing log record information.

        Returns:
            LogRecord: OpenTelemetry LogRecord object with mapped severity and attributes.
        """
        severity_number = SEVERITY_MAPPING.get(
            record["level"], SeverityNumber.UNSPECIFIED
        )

        # Start with base attributes
        attributes: Dict[str, Any] = {
            "code.filepath": record["file"],
            "code.function": record["function"],
            "code.lineno": record["line"],
            "level": record["level"],
        }

        # Add error code if present in extra
        if "extra" in record and "error_code" in record["extra"]:
            attributes["error.code"] = record["extra"]["error_code"]

        # Add extra attributes at the same level

        # Add extra attributes at the same level
        if "extra" in record:
            for key, value in record["extra"].items():
                if key != "error_code":  # Skip error_code as it's already handled
                    if isinstance(value, (bool, int, float, str, bytes)):
                        attributes[key] = value
                    else:
                        attributes[key] = str(value)

        return LogRecord(
            timestamp=int(record["timestamp"] * 1e9),
            observed_timestamp=time_ns(),
            trace_id=0,
            span_id=0,
            trace_flags=TraceFlags(0),
            severity_text=record["level"],
            severity_number=severity_number,
            body=record["message"],
            resource=self.logger_provider.resource,
            attributes=attributes,
        )

    def _start_asyncio_flush(self):
        """Start an asyncio event loop for periodic log flushing.

        Creates a new event loop and runs the periodic flush task in the background.
        This is used when no existing event loop is available.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            AtlanLoggerAdapter._flush_task = loop.create_task(self._periodic_flush())
            loop.run_forever()
        finally:
            loop.close()

    def process(self, msg: Any, kwargs: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
        """Process log message with temporal and request context.

        Args:
            msg (Any): Original log message
            kwargs (Dict[str, Any]): Additional logging parameters

        Returns:
            Tuple[Any, Dict[str, Any]]: Processed message and updated kwargs with context

        This method:
        - Adds request context if available
        - Adds workflow context if in a workflow
        - Adds activity context if in an activity
        """
        kwargs["logger_name"] = self.logger_name

        # Get request context
        try:
            ctx = request_context.get()
            if ctx and "request_id" in ctx:
                kwargs["request_id"] = ctx["request_id"]
        except Exception:
            pass

        workflow_context = get_workflow_context()

        try:
            if workflow_context and workflow_context.in_workflow == "true":
                # Only append workflow context if we have workflow info
                workflow_msg = f" Workflow Context: Workflow ID: {workflow_context.workflow_id} Run ID: {workflow_context.workflow_run_id} Type: {workflow_context.workflow_type}"
                msg = f"{msg}{workflow_msg}"
                kwargs.update(workflow_context.model_dump())
        except Exception:
            pass

        try:
            if workflow_context and workflow_context.in_activity == "true":
                # Only append activity context if we have activity info
                activity_msg = f" Activity Context: Activity ID: {workflow_context.activity_id} Workflow ID: {workflow_context.workflow_id} Run ID: {workflow_context.workflow_run_id} Type: {workflow_context.activity_type}"
                msg = f"{msg}{activity_msg}"
                kwargs.update(workflow_context.model_dump())
        except Exception:
            pass

        return msg, kwargs

    def debug(self, msg: str, *args: Any, **kwargs: Any):
        """Log a debug level message.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context
        """
        try:
            msg, kwargs = self.process(msg, kwargs)
            self.logger.bind(**kwargs).debug(msg, *args)
        except Exception as e:
            logging.error(f"Error in debug logging: {e}")
            self._sync_flush()

    def info(self, msg: str, *args: Any, **kwargs: Any):
        """Log an info level message.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context
        """
        try:
            msg, kwargs = self.process(msg, kwargs)
            self.logger.bind(**kwargs).info(msg, *args)
        except Exception as e:
            logging.error(f"Error in info logging: {e}")
            self._sync_flush()

    def warning(self, msg: str, *args: Any, **kwargs: Any):
        """Log a warning level message.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context
        """
        try:
            msg, kwargs = self.process(msg, kwargs)
            self.logger.bind(**kwargs).warning(msg, *args)
        except Exception as e:
            logging.error(f"Error in warning logging: {e}")
            self._sync_flush()

    def error(self, msg: str, *args: Any, **kwargs: Any):
        """Log an error level message.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context

        Note: Forces an immediate flush of logs when called.
        """
        try:
            msg, kwargs = self.process(msg, kwargs)
            self.logger.bind(**kwargs).error(msg, *args)
            # Force flush on error logs
            self._sync_flush()
        except Exception as e:
            logging.error(f"Error in error logging: {e}")
            self._sync_flush()

    def critical(self, msg: str, *args: Any, **kwargs: Any):
        """Log a critical level message.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context

        Note: Forces an immediate flush of logs when called.
        """
        try:
            msg, kwargs = self.process(msg, kwargs)
            self.logger.bind(**kwargs).critical(msg, *args)
            # Force flush on critical logs
            self._sync_flush()
        except Exception as e:
            logging.error(f"Error in critical logging: {e}")
            self._sync_flush()

    def activity(self, msg: str, *args: Any, **kwargs: Any):
        """Log an activity-specific message with activity context.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context

        This method adds activity-specific context to the log message.
        """
        try:
            local_kwargs = kwargs.copy()
            local_kwargs["log_type"] = "activity"
            processed_msg, processed_kwargs = self.process(msg, local_kwargs)
            self.logger.bind(**processed_kwargs).log("ACTIVITY", processed_msg, *args)
        except Exception as e:
            logging.error(f"Error in activity logging: {e}")
            self._sync_flush()

    def metric(self, msg: str, *args: Any, **kwargs: Any):
        """Log a metric-specific message with metric context.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context

        This method adds metric-specific context to the log message.
        """
        try:
            local_kwargs = kwargs.copy()
            local_kwargs["log_type"] = "metric"
            processed_msg, processed_kwargs = self.process(msg, local_kwargs)
            self.logger.bind(**processed_kwargs).log("METRIC", processed_msg, *args)
        except Exception as e:
            logging.error(f"Error in metric logging: {e}")
            self._sync_flush()

    def _send_to_otel(self, record: LogRecordModel):
        """Send log record to OpenTelemetry.

        Args:
            record (LogRecordModel): Log record to send

        This method:
        - Creates an OpenTelemetry LogRecord
        - Gets the logger from the provider
        - Emits the log record
        """
        try:
            # Create OpenTelemetry LogRecord
            otel_record = self._create_log_record(record.model_dump())

            # Get the logger from the provider
            logger = self.logger_provider.get_logger(SERVICE_NAME)

            # Emit the log record
            logger.emit(otel_record)
        except Exception as e:
            logging.error(f"Error sending log to OpenTelemetry: {e}")

    def _sync_flush(self):
        """Synchronously flush the log buffer.

        This method:
        - Attempts to use existing event loop if available
        - Creates new event loop if none exists
        - Ensures logs are flushed immediately
        """
        try:
            # Try to get the current event loop
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If we're in an async context, create a task
                    asyncio.create_task(self._flush_buffer(force=True))
                else:
                    # If we have a loop but it's not running, run the flush
                    loop.run_until_complete(self._flush_buffer(force=True))
            except RuntimeError:
                # If no event loop exists, create a new one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._flush_buffer(force=True))
                finally:
                    loop.close()
        except Exception as e:
            logging.error(f"Error during sync flush: {e}")

    def tracing(self, msg: str, *args: Any, **kwargs: Any):
        """Log a trace-specific message with trace context.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context

        This method adds trace-specific context to the log message.
        """
        local_kwargs = kwargs.copy()
        local_kwargs["log_type"] = "trace"
        processed_msg, processed_kwargs = self.process(msg, local_kwargs)
        self.logger.bind(**processed_kwargs).log("TRACING", processed_msg, *args)

    async def parquet_sink(self, message: Any):
        """Process log message and store in parquet format.

        Args:
            message (Any): Log message to process and store

        This method:
        - Creates a LogRecordModel from the message
        - Adds the record to the buffer for parquet storage
        """
        try:
            # Create LogRecordModel using the class method
            log_record = LogRecordModel.from_loguru_message(message)

            # Use base class's add_record method which handles buffering and flushing
            self.add_record(log_record)
        except Exception as e:
            logging.error(f"Error buffering log: {e}")

    def otlp_sink(self, message: Any):
        """Process log message and emit to OTLP.

        Args:
            message (Any): Log message to process and emit

        This method:
        - Creates a LogRecordModel from the message
        - Sends the record to OpenTelemetry
        """
        try:
            # Create LogRecordModel using the class method
            log_record = LogRecordModel.from_loguru_message(message)
            self._send_to_otel(log_record)
        except Exception as e:
            logging.error(f"Error processing log record: {e}")

    def __del__(self):
        """Cleanup when the logger is destroyed."""
        if AtlanLoggerAdapter._flush_task and not AtlanLoggerAdapter._flush_task.done():
            AtlanLoggerAdapter._flush_task.cancel()


# Create a singleton instance of the logger
_logger_instances: Dict[str, AtlanLoggerAdapter] = {}


def get_logger(name: str | None = None) -> AtlanLoggerAdapter:
    """Get or create an instance of AtlanLoggerAdapter.
    Args:
        name (str, optional): Logger name. If None, uses the caller's module name.
    Returns:
        AtlanLoggerAdapter: Logger instance for the specified name
    """
    global _logger_instances

    # If no name provided, use the caller's module name
    if name is None:
        name = __name__
    # Create new logger instance if it doesn't exist
    if name not in _logger_instances:
        _logger_instances[name] = AtlanLoggerAdapter(name)

    return _logger_instances[name]


# Initialize the default logger
default_logger = get_logger()  # Use a different name instead of redefining 'logger'
