import logging
import os
from contextvars import ContextVar
from typing import Any, MutableMapping, Tuple

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs._internal.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from temporalio import activity, workflow

# Create a context variable for request_id
request_context: ContextVar[dict] = ContextVar("request_context", default={})

SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "application-sdk")
SERVICE_VERSION: str = os.getenv("OTEL_SERVICE_VERSION", "0.1.0")
OTEL_EXPORTER_LOGS_ENDPOINT: str = os.getenv(
    "OTEL_EXPORTER_LOGS_ENDPOINT", "http://localhost:4318/v1/logs"
)
ENABLE_OTLP_LOGS: bool = os.getenv("ENABLE_OTLP_LOGS", "false").lower() == "true"


class AtlanLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger) -> None:
        """Create the logger adapter with enhanced configuration."""
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
                logger_provider = LoggerProvider(
                    resource=Resource.create(
                        {
                            "service.name": SERVICE_NAME,
                            "service.version": SERVICE_VERSION,
                            "k8s.log.type": "service-logs",
                        }
                    )
                )

                exporter = OTLPLogExporter(
                    endpoint=OTEL_EXPORTER_LOGS_ENDPOINT,
                    timeout=int(os.getenv("OTEL_EXPORTER_TIMEOUT_SECONDS", "30")),
                )
                batch_processor = BatchLogRecordProcessor(
                    exporter,
                    schedule_delay_millis=int(os.getenv("OTEL_BATCH_DELAY_MS", "5000")),
                    max_export_batch_size=int(os.getenv("OTEL_BATCH_SIZE", "512")),
                    max_queue_size=int(os.getenv("OTEL_QUEUE_SIZE", "2048")),
                )
                logger_provider.add_log_record_processor(batch_processor)

                otlp_handler = LoggingHandler(
                    level=logging.INFO,
                    logger_provider=logger_provider,
                )
                otlp_handler.setFormatter(workflow_formatter)

                logger.addHandler(otlp_handler)

        except Exception as e:
            print(f"Failed to setup logging: {str(e)}")
            # Fallback to basic console logging
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(simple_formatter)
            logger.addHandler(console_handler)

        super().__init__(logger, {})

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
