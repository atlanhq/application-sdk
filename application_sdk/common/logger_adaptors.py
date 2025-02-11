import os
import re
import sys
from contextvars import ContextVar
from typing import Any, MutableMapping, Tuple

from loguru import logger
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs._internal.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from temporalio import activity, workflow

ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

# Create a context variable for request_id
request_context: ContextVar[dict] = ContextVar("request_context", default={})

SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "application-sdk")
SERVICE_VERSION: str = os.getenv("OTEL_SERVICE_VERSION", "0.1.0")
OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
)
ENABLE_OTLP_LOGS: bool = os.getenv("ENABLE_OTLP_LOGS", "false").lower() == "true"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()


class AtlanLoggerAdapter:
    def __init__(self) -> None:
        """Create the logger adapter with enhanced configuration."""
        self.logger = logger.bind()
        logger.remove()  # Remove default handler

        # Add console handler with markup=True to interpret color tags
        format_str = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> <blue>[{level}]</blue> <cyan>{name}</cyan> - <level>{message}</level>"
        self.logger.add(sys.stderr, format=format_str, level=LOG_LEVEL, colorize=True)

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

                logger_provider = LoggerProvider(
                    resource=Resource.create(resource_attributes)
                )

                exporter = OTLPLogExporter(
                    endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
                    timeout=int(os.getenv("OTEL_EXPORTER_TIMEOUT_SECONDS", "30")),
                )

                # Create batch processor with custom emit method
                batch_processor = BatchLogRecordProcessor(
                    exporter,
                    schedule_delay_millis=int(os.getenv("OTEL_BATCH_DELAY_MS", "5000")),
                    max_export_batch_size=int(os.getenv("OTEL_BATCH_SIZE", "512")),
                    max_queue_size=int(os.getenv("OTEL_QUEUE_SIZE", "2048")),
                )

                logger_provider.add_log_record_processor(batch_processor)

                # Create sink for OTLP
                def otlp_sink(message):
                    record = message.record
                    # Define our own level mapping
                    level_map = {
                        "DEBUG": 10,
                        "INFO": 20,
                        "WARNING": 30,
                        "ERROR": 40,
                        "CRITICAL": 50,
                    }
                    logging_level = level_map.get(
                        LOG_LEVEL, 20
                    )  # Default to INFO level (20)

                    handler = LoggingHandler(
                        level=logging_level,
                        logger_provider=logger_provider,
                    )
                    handler.emit(record)

                # Add OTLP sink
                logger.add(otlp_sink, level=LOG_LEVEL)

            except Exception as e:
                print(f"Failed to setup OTLP logging: {str(e)}")

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        """Process the log message with temporal context."""
        extra = kwargs.get("extra", {})

        # Get request context
        try:
            ctx = request_context.get()
            if ctx and "request_id" in ctx:
                extra["request_id"] = ctx["request_id"]
        except Exception:
            pass

        # Get temporal context
        try:
            workflow_info = workflow.info()
            if workflow_info:
                extra.update(
                    {
                        "workflow_id": workflow_info.workflow_id,
                        "run_id": workflow_info.run_id,
                        "workflow_type": workflow_info.workflow_type,
                        "namespace": workflow_info.namespace,
                        "task_queue": workflow_info.task_queue,
                        "attempt": workflow_info.attempt,
                    }
                )
                workflow_context = (
                    "\nWorkflow Context:"
                    f"\n  Workflow ID: <m>{workflow_info.workflow_id}</m>"
                    f"\n  Run ID: <b>{workflow_info.run_id}</b>"
                    f"\n  Type: <g>{workflow_info.workflow_type}</g>"
                )
                msg = f"{msg}{workflow_context}"
        except Exception:
            pass

        try:
            activity_info = activity.info()
            if activity_info:
                extra.update(
                    {
                        "workflow_id": activity_info.workflow_id,
                        "run_id": activity_info.workflow_run_id,
                        "activity_id": activity_info.activity_id,
                        "activity_type": activity_info.activity_type,
                        "task_queue": activity_info.task_queue,
                        "attempt": activity_info.attempt,
                        "schedule_to_close_timeout": str(
                            activity_info.schedule_to_close_timeout
                        ),
                        "start_to_close_timeout": str(
                            activity_info.start_to_close_timeout
                        ),
                    }
                )
                activity_context = (
                    "\nActivity Context:"
                    f"\n  Activity ID: {activity_info.activity_id}"
                    f"\n  Workflow ID: <m>{activity_info.workflow_id}</m>"
                    f"\n  Run ID: <b>{activity_info.workflow_run_id}</b>"
                    f"\n  Type: <g>{activity_info.activity_type}</g>"
                )
                msg = f"{msg}{activity_context}"
        except Exception:
            pass

        kwargs["extra"] = extra
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

    if name not in _logger_instances:
        _logger_instances[name] = AtlanLoggerAdapter()
    return _logger_instances[name]


# Initialize the default logger
default_logger = get_logger()  # Use a different name instead of redefining 'logger'
