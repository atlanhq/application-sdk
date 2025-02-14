import os
import sys
from contextvars import ContextVar
from time import time_ns
from typing import Any, MutableMapping, Tuple

from loguru import logger
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LogRecord
from opentelemetry.sdk._logs._internal.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from temporalio import activity, workflow

# Create a context variable for request_id
request_context: ContextVar[dict] = ContextVar("request_context", default={})

SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "application-sdk")
SERVICE_VERSION: str = os.getenv("OTEL_SERVICE_VERSION", "0.1.0")
OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
)
ENABLE_OTLP_LOGS: bool = os.getenv("ENABLE_OTLP_LOGS", "false").lower() == "true"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# Add these constants
SEVERITY_MAPPING = {
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "WARNING": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
    "CRITICAL": SeverityNumber.FATAL,
}


class AtlanLoggerAdapter:
    def __init__(self, logger_name: str) -> None:
        """Create the logger adapter with enhanced configuration."""
        self.logger_name = logger_name
        # Bind the logger name when creating the logger instance
        self.logger = logger
        logger.remove()

        # Update format string to use the bound logger_name
        atlan_format_str = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> <blue>[{level}]</blue> <cyan>{extra[logger_name]}</cyan> - <level>{message}</level>"
        self.logger.add(
            sys.stderr, format=atlan_format_str, level=LOG_LEVEL, colorize=True
        )

        # Store the color-enabled logger instance
        self.logger = self.logger.opt(colors=True)

        # OTLP handler setup
        if ENABLE_OTLP_LOGS:
            try:
                # Get workflow node name for Argo environment
                workflow_node_name = os.getenv("OTEL_WF_NODE_NAME", "")

                # Base resource attributes
                resource_attributes = {
                    "service.name": SERVICE_NAME,
                    "service.version": SERVICE_VERSION,
                }

                # Add workflow node name if running in Argo
                if workflow_node_name:
                    resource_attributes["k8s.workflow.node.name"] = workflow_node_name

                self.logger_provider = LoggerProvider(
                    resource=Resource.create(resource_attributes)
                )

                exporter = OTLPLogExporter(
                    endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
                    timeout=int(os.getenv("OTEL_EXPORTER_TIMEOUT_SECONDS", "30")),
                )

                batch_processor = BatchLogRecordProcessor(
                    exporter,
                    schedule_delay_millis=int(os.getenv("OTEL_BATCH_DELAY_MS", "5000")),
                    max_export_batch_size=int(os.getenv("OTEL_BATCH_SIZE", "512")),
                    max_queue_size=int(os.getenv("OTEL_QUEUE_SIZE", "2048")),
                )

                self.logger_provider.add_log_record_processor(batch_processor)

                # Add OTLP sink
                self.logger.add(self.otlp_sink, level=LOG_LEVEL)

            except Exception as e:
                self.logger.error(f"Failed to setup OTLP logging: {str(e)}")

    def _create_log_record(self, record: dict) -> LogRecord:
        """Create an OpenTelemetry LogRecord."""
        severity_number = SEVERITY_MAPPING.get(
            record["level"].name, SeverityNumber.UNSPECIFIED
        )

        # Start with base attributes
        attributes = {
            "code.filepath": str(record["file"].path),
            "code.function": str(record["function"]),
            "code.lineno": int(record["line"]),
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
            trace_flags=0,
            severity_text=record["level"].name,
            severity_number=severity_number,
            body=record["message"],
            resource=self.logger_provider.resource,
            attributes=attributes,
        )

    def otlp_sink(self, message):
        """Process log message and emit to OTLP."""
        try:
            log_record = self._create_log_record(message.record)
            self.logger_provider.get_logger(SERVICE_NAME).emit(log_record)
        except Exception as e:
            self.logger.error(f"Error processing log record: {e}")

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
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
                kwargs.update(
                    {
                        "workflow_id": workflow_info.workflow_id if workflow_info.workflow_id else "",
                        "run_id": workflow_info.run_id if workflow_info.run_id else "",
                        "workflow_type": workflow_info.workflow_type if workflow_info.workflow_type else "",
                        "namespace": workflow_info.namespace if workflow_info.namespace else "",
                        "task_queue": workflow_info.task_queue if workflow_info.task_queue else "",
                        "attempt": workflow_info.attempt if workflow_info.attempt else 0,
                    }
                )
                workflow_context = "Workflow Context: Workflow ID: <m>{workflow_info.workflow_id}</m> Run ID: <e>{workflow_info.run_id}</e> Type: <g>{workflow_info.workflow_type}</g>"
                msg = f"{msg} {workflow_context}"
        except Exception:
            pass

        try:
            activity_info = activity.info()
            if activity_info:
                kwargs.update(
                    {
                        "workflow_id": activity_info.workflow_id if activity_info.workflow_id else "",
                        "run_id": activity_info.workflow_run_id if activity_info.workflow_run_id else "",
                        "activity_id": activity_info.activity_id if activity_info.activity_id else "",
                        "activity_type": activity_info.activity_type if activity_info.activity_type else "",
                        "task_queue": activity_info.task_queue if activity_info.task_queue else "",
                        "attempt": activity_info.attempt if activity_info.attempt else 0,
                        "schedule_to_close_timeout": str(
                            activity_info.schedule_to_close_timeout if activity_info.schedule_to_close_timeout else 0
                        ),
                        "start_to_close_timeout": str(
                            activity_info.start_to_close_timeout if activity_info.start_to_close_timeout else 0
                        ),
                    }
                )
                activity_context = "Activity Context: Activity ID: {activity_info.activity_id} Workflow ID: <m>{activity_info.workflow_id}</m> Run ID: <e>{activity_info.workflow_run_id}</e> Type: <g>{activity_info.activity_type}</g>"
                msg = f"{msg} {activity_context}"
        except Exception:
            pass

        return msg, kwargs

    def debug(self, msg: str, *args, **kwargs):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.error(msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs):
        msg, kwargs = self.process(msg, kwargs)
        self.logger.critical(msg, *args, **kwargs)


# Create a singleton instance of the logger
_logger_instances = {}


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
