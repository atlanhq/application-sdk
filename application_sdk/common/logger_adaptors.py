import logging
import sys
from contextvars import ContextVar
from time import time_ns
from typing import Any, Dict, Tuple

from loguru import logger
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LogRecord
from opentelemetry.sdk._logs._internal.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace.span import TraceFlags
from temporalio import activity, workflow

from application_sdk.constants import (
    ENABLE_OTLP_LOGS,
    LOG_LEVEL,
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

# Create a context variable for request_id
request_context: ContextVar[Dict[str, Any]] = ContextVar("request_context", default={})


# Add a Loguru handler for the Python logging system
class InterceptHandler(logging.Handler):
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
}


class AtlanLoggerAdapter:
    def __init__(self, logger_name: str) -> None:
        """Create the logger adapter with enhanced configuration."""
        self.logger_name = logger_name
        # Bind the logger name when creating the logger instance
        self.logger = logger
        logger.remove()

        # Register custom log level for activity
        if "ACTIVITY" not in logger._core.levels:
            logger.level("ACTIVITY", no=20, color="<cyan>", icon="🔵")

        # Update format string to use the bound logger_name
        atlan_format_str = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> <blue>[{level}]</blue> <cyan>{extra[logger_name]}</cyan> - <level>{message}</level>"
        self.logger.add(
            sys.stderr, format=atlan_format_str, level=LOG_LEVEL, colorize=True
        )

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
                self.logger.error(f"Failed to setup OTLP logging: {str(e)}")

    def _parse_otel_resource_attributes(self, env_var: str) -> dict[str, str]:
        try:
            # Check if the environment variable is not empty
            if env_var:
                # Split the string by commas to get individual key-value pairs
                attributes = env_var.split(",")
                # Create a dictionary from the key-value pairs
                return {
                    item.split("=")[0].strip(): item.split("=")[
                        1
                    ].strip()  # Strip spaces around the key and value
                    for item in attributes
                    if "=" in item  # Ensure there's an "=" to split
                }
        except Exception as e:
            self.logger.error(f"Failed to parse OTLP resource attributes: {str(e)}")
        return {}

    def _create_log_record(self, record: dict) -> LogRecord:
        """Create an OpenTelemetry LogRecord."""
        severity_number = SEVERITY_MAPPING.get(
            record["level"].name, SeverityNumber.UNSPECIFIED
        )

        # Start with base attributes
        attributes: Dict[str, Any] = {
            "code.filepath": str(record["file"].path),
            "code.function": str(record["function"]),
            "code.lineno": int(record["line"]),
            "level": record["level"].name,
        }

        # Add extra attributes at the same level
        if "extra" in record:
            for key, value in record["extra"].items():
                if isinstance(value, (bool, int, float, str, bytes)):
                    attributes[key] = value
                else:
                    attributes[key] = str(value)

        return LogRecord(
            timestamp=int(record["time"].timestamp() * 1e9),
            observed_timestamp=time_ns(),
            trace_id=0,
            span_id=0,
            trace_flags=TraceFlags(0),
            severity_text=record["level"].name,
            severity_number=severity_number,
            body=record["message"],
            resource=self.logger_provider.resource,
            attributes=attributes,
        )

    def otlp_sink(self, message: Any):
        """Process log message and emit to OTLP."""
        try:
            log_record = self._create_log_record(message.record)
            self.logger_provider.get_logger(SERVICE_NAME).emit(log_record)
        except Exception as e:
            self.logger.error(f"Error processing log record: {e}")

    def process(self, msg: Any, kwargs: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
        """Process the log message with temporal context."""
        kwargs["logger_name"] = self.logger_name

        # Get request context
        try:
            ctx = request_context.get()
            if ctx and "request_id" in ctx:
                kwargs["request_id"] = ctx["request_id"]
        except Exception:
            pass

        try:
            workflow_info = workflow.info()
            if workflow_info:
                workflow_context = {
                    "workflow_id": workflow_info.workflow_id or "",
                    "run_id": workflow_info.run_id or "",
                    "workflow_type": workflow_info.workflow_type or "",
                    "namespace": workflow_info.namespace or "",
                    "task_queue": workflow_info.task_queue or "",
                    "attempt": workflow_info.attempt or 0,
                }
                kwargs.update(workflow_context)

                # Only append workflow context if we have workflow info
                workflow_msg = " Workflow Context: Workflow ID: {workflow_id} Run ID: {run_id} Type: {workflow_type}"
                msg = f"{msg}{workflow_msg}"
        except Exception:
            pass

        try:
            activity_info = activity.info()
            if activity_info:
                activity_context = {
                    "workflow_id": activity_info.workflow_id or "",
                    "run_id": activity_info.workflow_run_id or "",
                    "activity_id": activity_info.activity_id or "",
                    "activity_type": activity_info.activity_type or "",
                    "task_queue": activity_info.task_queue or "",
                    "attempt": activity_info.attempt or 0,
                    "schedule_to_close_timeout": str(
                        activity_info.schedule_to_close_timeout or 0
                    ),
                    "start_to_close_timeout": str(
                        activity_info.start_to_close_timeout or None
                    ),
                    "schedule_to_start_timeout": str(
                        activity_info.schedule_to_start_timeout or None
                    ),
                    "heartbeat_timeout": str(activity_info.heartbeat_timeout or None),
                }
                kwargs.update(activity_context)

                # Only append activity context if we have activity info
                activity_msg = " Activity Context: Activity ID: {activity_id} Workflow ID: {workflow_id} Run ID: {run_id} Type: {activity_type}"
                msg = f"{msg}{activity_msg}"
        except Exception:
            pass

        return msg, kwargs

    def debug(self, msg: str, *args: Any, **kwargs: Any):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.bind(**kwargs).debug(msg, *args)

    def info(self, msg: str, *args: Any, **kwargs: Any):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.bind(**kwargs).info(msg, *args)

    def warning(self, msg: str, *args: Any, **kwargs: Any):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.bind(**kwargs).warning(msg, *args)

    def error(self, msg: str, *args: Any, **kwargs: Any):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.bind(**kwargs).error(msg, *args)

    def critical(self, msg: str, *args: Any, **kwargs: Any):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.bind(**kwargs).critical(msg, *args)

    def activity(self, msg: str, *args: Any, **kwargs: Any):
        """Log an activity-specific message with activity context.

        This method is specifically designed for logging activity-related information.
        It automatically adds activity context if available and formats the message
        with activity-specific information.

        Args:
            msg: The message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments
        """
        # Create a copy to avoid modifying the original dict directly
        # and potentially help type checker inference.
        local_kwargs = kwargs.copy()
        local_kwargs["log_type"] = "activity"

        # Process the message with context using the copied dict
        processed_msg, processed_kwargs = self.process(msg, local_kwargs)

        # Log with the custom ACTIVITY level using the processed dict
        self.logger.bind(**processed_kwargs).log("ACTIVITY", processed_msg, *args)


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
