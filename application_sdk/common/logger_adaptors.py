import logging
import os
import threading
from typing import Any, MutableMapping, Tuple

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs._internal.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

from temporalio import activity, workflow

SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "unknown")
SERVICE_VERSION: str = os.getenv("OTEL_SERVICE_VERSION", "unknown")
OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")


class AtlanLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    def __init__(self, logger: logging.Logger) -> None:
        """Create the logger adapter."""
        try:
            logger_provider = LoggerProvider(Resource.create({
                "service.name": SERVICE_NAME,
                "service.version": SERVICE_VERSION,
                "host.name": os.getenv("ATLAN_DOMAIN", "ENV_NOT_SET"),
            }))
            exporter = OTLPLogExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT)
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
            handler = LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider)
            logger.addHandler(handler)
        except Exception as e:
            logger.error(e)

        super().__init__(logger, {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        extra = {}
        extra["thread_id"] = str(threading.get_ident())
        extra["process_id"] = str(os.getpid())

        # Fetch workflow information if within the workflow context
        try:
            workflow_info = workflow.info()
            if workflow_info:
                extra["run_id"] = workflow_info.run_id
                extra["workflow_id"] = workflow_info.workflow_id
                extra["workflow_namespace"] = workflow_info.namespace
                extra["task_queue"] = workflow_info.task_queue
                extra["workflow_type"] = workflow_info.workflow_type
        except Exception:
            pass

        # Fetch activity information if within the activity context
        try:
            activity_info = activity.info()
            if activity_info:
                extra["workflow_id"] = activity_info.workflow_id
                extra["run_id"] = activity_info.workflow_run_id
                extra["activity_id"] = activity_info.activity_id
                extra["activity_type"] = activity_info.activity_type
                extra["workflow_namespace"] = activity_info.workflow_namespace
                extra["task_queue"] = activity_info.task_queue
        except Exception:
            pass

        kwargs["extra"] = extra

        return (msg, kwargs)

    def isEnabledFor(self, level: int) -> bool:
        """Override to ignore replay logs."""
        return super().isEnabledFor(level)

    @property
    def base_logger(self) -> logging.Logger:
        """Underlying logger usable for actions such as adding
        handlers/formatters.
        """
        return self.logger
