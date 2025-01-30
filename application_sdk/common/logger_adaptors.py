import logging
import os
from contextvars import ContextVar
from typing import Any, MutableMapping, Tuple

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs._internal.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from temporalio import activity, workflow

SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "unknown")
SERVICE_VERSION: str = os.getenv("OTEL_SERVICE_VERSION", "1.0.0")
OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/logs"
)

# Create a context variable for request_id
request_context: ContextVar[dict] = ContextVar("request_context", default={})


class AtlanLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    def __init__(self, logger: logging.Logger) -> None:
        """Create the logger adapter with enhanced configuration."""
        logger.setLevel(logging.INFO)

        try:
            # Create a more detailed formatter that handles missing fields
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s - %(message)s "
                "[request_id=%(request_id)s] "
                "[event_type=%(event_type)s] "
                "[workflow_id=%(workflow_id)s] "
                "[run_id=%(run_id)s] "
                "[activity_id=%(activity_id)s] "
                "[workflow_namespace=%(workflow_namespace)s] "
                "[task_queue=%(task_queue)s] "
                "[workflow_type=%(workflow_type)s] ",
                defaults={
                    "request_id": "N/A",
                    "event_type": "N/A",
                    "workflow_id": "N/A",
                    "run_id": "N/A",
                    "activity_id": "N/A",
                    "workflow_namespace": "N/A",
                    "task_queue": "N/A",
                    "workflow_type": "N/A",
                },
            )

            # Check if we're in a development environment
            is_development = (
                os.getenv("ENVIRONMENT", "development").lower() == "development"
            )

            if is_development:
                # In development, only use console handler
                console_handler = logging.StreamHandler()
                console_handler.setLevel(logging.INFO)
                console_handler.setFormatter(formatter)
                logger.addHandler(console_handler)
            else:
                # In production, use OTLP handler
                logger_provider = LoggerProvider(
                    resource=Resource.create(
                        {
                            "service.name": os.getenv(
                                "OTEL_SERVICE_NAME", "postgresql-application"
                            ),
                            "service.version": os.getenv(
                                "OTEL_SERVICE_VERSION", "1.0.0"
                            ),
                            "host.name": os.getenv("ATLAN_DOMAIN", "ENV_NOT_SET"),
                            "k8s.log.type": "service-logs",
                        }
                    )
                )

                exporter = OTLPLogExporter(
                    endpoint=os.getenv(
                        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/logs"
                    ),
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
                otlp_handler.setFormatter(formatter)
                logger.addHandler(otlp_handler)

        except Exception as e:
            print(f"Failed to setup OTLP logging: {str(e)}")
            # Fallback to basic console logging with the same formatter
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        super().__init__(logger, {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        """Enhanced process method with additional context."""
        if "extra" not in kwargs:
            kwargs["extra"] = {}
        extra = kwargs["extra"]

        # Initialize default values
        extra.setdefault("event_type", "N/A")
        extra.setdefault("request_id", "N/A")
        extra.setdefault("workflow_id", "N/A")
        extra.setdefault("run_id", "N/A")
        extra.setdefault("activity_id", "N/A")
        extra.setdefault("workflow_namespace", "N/A")
        extra.setdefault("task_queue", "N/A")
        extra.setdefault("workflow_type", "N/A")

        # Add request context if available
        try:
            ctx = request_context.get()
            if "request_id" in ctx:
                extra["request_id"] = ctx["request_id"]
        except LookupError:
            pass

        # Add workflow context if available
        try:
            workflow_info = workflow.info()
            if workflow_info:
                extra.update(
                    {
                        "run_id": workflow_info.run_id,
                        "workflow_id": workflow_info.workflow_id,
                        "workflow_namespace": workflow_info.namespace,
                        "task_queue": workflow_info.task_queue,
                        "workflow_type": workflow_info.workflow_type,
                    }
                )
        except Exception:
            pass

        # Add activity context if available
        try:
            activity_info = activity.info()
            if activity_info:
                extra.update(
                    {
                        "workflow_id": activity_info.workflow_id,
                        "run_id": activity_info.workflow_run_id,
                        "activity_id": activity_info.activity_id,
                        "activity_type": activity_info.activity_type,
                        "workflow_namespace": activity_info.workflow_namespace,
                        "task_queue": activity_info.task_queue,
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
