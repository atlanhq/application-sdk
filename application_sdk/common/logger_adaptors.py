import logging
import os
from contextvars import ContextVar
from typing import Any, MutableMapping, Tuple

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs._internal.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from temporalio import activity, workflow

SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "application-sdk")
SERVICE_VERSION: str = os.getenv("OTEL_SERVICE_VERSION", "1.0.0")
OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/logs"
)

# Create a context variable for request_id
request_context: ContextVar[dict] = ContextVar("request_context", default={})


class AtlanLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger) -> None:
        """Create the logger adapter with enhanced configuration."""
        logger.setLevel(logging.INFO)

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s "
            "[workflow_id=%(workflow_id)s] "
            "[run_id=%(run_id)s] "
            "[activity_id=%(activity_id)s] "
            "[workflow_type=%(workflow_type)s] ",
            defaults={
                "workflow_id": "N/A",
                "run_id": "N/A",
                "activity_id": "N/A",
                "workflow_type": "N/A",
            },
        )

        # Setup handlers based on environment
        is_development = (
            os.getenv("ENVIRONMENT", "development").lower() == "development"
        )

        try:
            if is_development:
                # In development, only use console handler
                console_handler = logging.StreamHandler()
                console_handler.setLevel(logging.INFO)
                console_handler.setFormatter(formatter)
                logger.addHandler(console_handler)
            else:
                # Production OTLP setup
                logger_provider = LoggerProvider(
                    resource=Resource.create(
                        {
                            "service.name": SERVICE_NAME,
                            "service.version": SERVICE_VERSION,
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
        """Process the log message with temporal context."""
        if "extra" not in kwargs:
            kwargs["extra"] = {}

        # Get temporal context
        try:
            workflow_info = workflow.info()
            if workflow_info:
                kwargs["extra"].update(
                    {
                        "workflow_id": workflow_info.workflow_id,
                        "run_id": workflow_info.run_id,
                        "workflow_type": workflow_info.workflow_type,
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
