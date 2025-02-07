import logging
import os
import re
from contextvars import ContextVar
from typing import Any, MutableMapping, Tuple

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

COLORS = {
    "DEBUG": "\033[94m",  # Blue
    "INFO": "\033[92m",  # Green
    "WARNING": "\033[93m",  # Yellow
    "ERROR": "\033[91m",  # Red
    "CRITICAL": "\033[91m",  # Red
    "WORKFLOW_ID": "\033[96m",  # Cyan
    "RUN_ID": "\033[95m",  # Magenta
    "ENDC": "\033[0m",  # Reset color
}


class AtlanLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger) -> None:
        """Create the logger adapter with enhanced configuration."""
        # Remove any existing handlers
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        logger.setLevel(logging.INFO)

        # Create OTLP formatter with detailed format for workflow/activity logs
        workflow_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s "
            "[workflow_id=%(workflow_id)s] "
            "[run_id=%(run_id)s] "
            "[activity_id=%(activity_id)s] "
            "[workflow_type=%(workflow_type)s]",
            defaults={
                "workflow_id": "N/A",
                "run_id": "N/A",
                "activity_id": "N/A",
                "workflow_type": "N/A",
            },
        )

        # Create simple formatter for regular logs
        simple_formatter = logging.Formatter("%(message)s")

        try:
            # Console handler with workflow formatter for workflow/activity logs
            workflow_handler = logging.StreamHandler()
            workflow_handler.setLevel(logging.INFO)
            workflow_handler.setFormatter(workflow_formatter)
            workflow_handler.addFilter(
                lambda record: hasattr(record, "workflow_id")
                or hasattr(record, "activity_id")
                or "workflow" in record.name.lower()
                or "activity" in record.name.lower()
            )
            logger.addHandler(workflow_handler)

            # Console handler with simple format for other logs
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)

            console_handler.setFormatter(simple_formatter)
            console_handler.addFilter(
                lambda record: not (
                    hasattr(record, "workflow_id")
                    or hasattr(record, "activity_id")
                    or "workflow" in record.name.lower()
                    or "activity" in record.name.lower()
                )
            )

            logger.addHandler(console_handler)

            # OTLP handler setup
            if ENABLE_OTLP_LOGS:
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

                # Monkey patch the emit method to handle different types
                original_emit = batch_processor.emit

                def custom_emit(log_data):
                    if not self._is_valid_type(log_data.log_record.body):
                        log_data.log_record.body = str(log_data.log_record.body)
                    original_emit(log_data)

                batch_processor.emit = custom_emit

                logger_provider.add_log_record_processor(batch_processor)

                otlp_handler = LoggingHandler(
                    level=logging.INFO,
                    logger_provider=logger_provider,
                )
                otlp_handler.setFormatter(workflow_formatter)
                otlp_handler.addFilter(
                    lambda record: setattr(
                        record, "msg", ansi_escape.sub("", record.msg)
                    )
                    or True
                )
                logger.addHandler(otlp_handler)

        except Exception as e:
            print(f"Failed to setup logging: {str(e)}")
            # Fallback to basic console logging
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(simple_formatter)
            logger.addHandler(console_handler)

        super().__init__(logger, {})

    def _is_valid_type(self, value):
        """Helper method to check if a value is of a valid type for OTLP logging."""
        if isinstance(value, (bool, str, int, float)):
            return True
        if isinstance(value, (list, tuple)):
            return all(self._is_valid_type(v) for v in value)
        if isinstance(value, dict):
            return all(
                self._is_valid_type(k) and self._is_valid_type(v)
                for k, v in value.items()
            )
        return False

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        """Process the log message with temporal context."""
        if "extra" not in kwargs:
            kwargs["extra"] = {}

        # Get request context
        try:
            ctx = request_context.get()
            if ctx and "request_id" in ctx:
                kwargs["extra"]["request_id"] = ctx["request_id"]
        except Exception:
            pass

        # Get temporal context with more verbose logging
        try:
            workflow_info = workflow.info()
            if workflow_info:
                kwargs["extra"].update(
                    {
                        "workflow_id": workflow_info.workflow_id,
                        "run_id": workflow_info.run_id,
                        "workflow_type": workflow_info.workflow_type,
                        "namespace": workflow_info.namespace,
                        "task_queue": workflow_info.task_queue,
                        "attempt": workflow_info.attempt,
                    }
                )
                msg += f" \n Workflow Info: \n [{COLORS['WORKFLOW_ID']}workflow_id={workflow_info.workflow_id}{COLORS['ENDC']}] [{COLORS['RUN_ID']}run_id={workflow_info.run_id}{COLORS['ENDC']}] [{COLORS['INFO']}workflow_type={workflow_info.workflow_type}{COLORS['ENDC']}] \n"
        except Exception:
            pass

        try:
            activity_info = activity.info()
            if activity_info:
                kwargs["extra"].update(
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
                msg += f" \n Activity Info: \n [{COLORS['WORKFLOW_ID']}workflow_id={activity_info.workflow_id}{COLORS['ENDC']}] [{COLORS['RUN_ID']}run_id={activity_info.workflow_run_id}{COLORS['ENDC']}][{COLORS['INFO']}activity_type={activity_info.activity_type}{COLORS['ENDC']}] \n"
        except Exception:
            pass

        return msg, kwargs

    def isEnabledFor(self, level: int) -> bool:
        """Override to ignore replay logs."""
        return super().isEnabledFor(level)

    @property
    def base_logger(self) -> logging.Logger:
        """Underlying logger usable for actions such as adding
        handlers/formatters.
        """
        return self.logger


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
        _logger_instances[name] = AtlanLoggerAdapter(logging.getLogger(name))
    return _logger_instances[name]

# Initialize the default logger
logger = get_logger()

