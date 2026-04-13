import asyncio
import logging
import sys
import threading
import traceback as tb_module
from time import time_ns
from typing import Any, ClassVar, Dict, Tuple

from loguru import logger
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs._internal.export import BatchLogRecordProcessor
from opentelemetry.trace.span import TraceFlags

from application_sdk.constants import (
    APPLICATION_NAME,
    ENABLE_OBSERVABILITY_STORE_SINK,
    ENABLE_OTLP_LOGS,
    ENABLE_OTLP_WORKFLOW_LOGS,
    LOG_BATCH_SIZE,
    LOG_CLEANUP_ENABLED,
    LOG_FILE_NAME,
    LOG_FLUSH_INTERVAL_SECONDS,
    LOG_LEVEL,
    LOG_RETENTION_DAYS,
    OTEL_BATCH_DELAY_MS,
    OTEL_BATCH_SIZE,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_TIMEOUT_SECONDS,
    OTEL_QUEUE_SIZE,
    OTEL_WORKFLOW_LOGS_ENDPOINT,
    SERVICE_NAME,
)
from application_sdk.observability.context import correlation_context, request_context
from application_sdk.observability.observability import AtlanObservability
from application_sdk.observability.utils import (
    build_otel_resource,
    get_observability_dir,
    get_workflow_context,
)
from application_sdk.version import __version__ as _SDK_VERSION

_KNOWN_EXTRA_KEYS = frozenset(
    {
        "client_host",
        "duration_ms",
        "method",
        "path",
        "request_id",
        "status_code",
        "url",
        "workflow_id",
        "run_id",
        "workflow_run_id",  # App Vitals uses this alias alongside run_id
        "workflow_type",
        "namespace",
        "task_queue",
        "attempt",
        "activity_id",
        "activity_type",
        "schedule_to_close_timeout",
        "start_to_close_timeout",
        "schedule_to_start_timeout",
        "heartbeat_timeout",
        "log_type",
        "app_name",
        "trace_id",
        "span_id",
        "correlation_id",
        # ── App Vitals ───────────────────────────────────────────────────
        # Identity
        "app_version",
        "tenant_id",
        # Outcome
        "status",
        "error_type",
        "error_class",
        "error_message",
        "is_retriable",
        "error_cause_chain",
        # Metric classification
        "dimension",
        "source",
        "metric_name",
        # Throughput
        "assets_processed",
        # Efficiency (per-activity resource deltas)
        "cpu_seconds",
        "mem_gb_sec",
        # Performance timings
        "schedule_to_start_ms",
        "timeout_budget_total_ms",
        "timeout_budget_used_pct",
        # Workflow summary (app_vitals.wf.summary event)
        "total_activities",
        "succeeded_activities",
        "failed_activities",
        "total_child_workflows",
        "first_failure_activity_type",
        "first_failure_error_type",
        "bottleneck_activity_type",
        "bottleneck_duration_ms",
    }
)


def _build_extra_dict(
    record_extra: Dict[str, Any], exception: Any = None
) -> Dict[str, Any]:
    """Build a dict of structured log extra fields from a loguru record's extra dict."""
    extra: Dict[str, Any] = {}
    for k, v in record_extra.items():
        if k != "logger_name" and k in _KNOWN_EXTRA_KEYS:
            extra[k] = _normalize_log_extra_value(k, v)
        elif (
            k.startswith("atlan-")
            or k.startswith("exception.")
            or k.startswith("temporal.")
            or k.startswith("tenant.")
        ) and v is not None:
            extra[k] = v if isinstance(v, (bool, int, float, str, bytes)) else str(v)
    for key, value in _extract_exception_attributes(exception).items():
        extra[key] = value
    return extra


def _make_log_record_dict(message: Any) -> Dict[str, Any]:
    """Build a log record dict from a loguru message."""
    return {
        "timestamp": message.record["time"].timestamp(),
        "level": message.record["level"].name,
        "logger_name": message.record["extra"].get("logger_name", ""),
        "message": message.record["message"],
        "file": str(message.record["file"].path),
        "line": message.record["line"],
        "function": message.record["function"],
        "extra": _build_extra_dict(
            message.record["extra"], message.record.get("exception")
        ),
    }


def _format_exception_stacktrace(exception: Any) -> str:
    """Format a Loguru exception record into a traceback string."""
    if exception is None:
        return ""
    exc_type = getattr(exception, "type", None)
    exc_value = getattr(exception, "value", None)
    exc_traceback = getattr(exception, "traceback", None)
    if exc_type is None:
        return ""
    return "".join(
        tb_module.format_exception(exc_type, exc_value, exc_traceback)
    ).rstrip()


def _extract_exception_attributes(exception: Any) -> Dict[str, str]:
    """Extract OTEL semantic exception attributes from a Loguru exception record."""
    if exception is None:
        return {}

    exc_type = getattr(exception, "type", None)
    exc_value = getattr(exception, "value", None)
    if exc_type is None:
        return {}

    module = getattr(exc_type, "__module__", None)
    qualname = getattr(exc_type, "__qualname__", getattr(exc_type, "__name__", None))
    type_name = f"{module}.{qualname}" if module and qualname else str(exc_type)

    attrs: Dict[str, str] = {"exception.type": type_name}
    if exc_value is not None:
        attrs["exception.message"] = str(exc_value)

    stacktrace = _format_exception_stacktrace(exception)
    if stacktrace:
        attrs["exception.stacktrace"] = stacktrace

    return attrs


def _normalize_log_extra_value(key: str, value: Any) -> Any:
    """Normalize known structured log fields to their canonical types."""
    if key == "attempt" and value is not None:
        return str(value)
    return value


def _format_printf_args(msg: str, args: Tuple[Any, ...]) -> Tuple[str, Tuple[Any, ...]]:
    """Pre-format printf-style args into the message string.

    Loguru uses {} formatting, not %s. This bridges the gap so both styles work
    without silent data loss.

    Returns (formatted_message, remaining_args). When %s substitution succeeds,
    args is emptied (loguru receives no positional args). When it fails (e.g.,
    {} placeholders), the original args are returned for loguru to handle.
    """
    if args:
        try:
            return msg % args, ()
        except (TypeError, ValueError):
            pass  # {} style or mismatch — let loguru handle it
    return msg, args


def _has_remote_otlp_endpoint() -> bool:
    """True when OTEL_EXPORTER_OTLP_ENDPOINT points to a real remote collector."""
    try:
        ep = OTEL_EXPORTER_OTLP_ENDPOINT.strip()
        if not ep:
            return False
        from urllib.parse import urlparse

        host = urlparse(ep).hostname or ""
        return host not in ("", "localhost", "127.0.0.1", "::1")
    except Exception:
        logging.debug("OTEL endpoint check failed, treating as local", exc_info=True)
        return False


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

DEPENDENCY_LOGGERS = ["daft_io.stats", "tracing.span", "httpx"]

# Configure external dependency loggers to reduce noise
# Set httpx to WARNING to reduce verbose HTTP request logs (200 OK messages)
for logger_name in DEPENDENCY_LOGGERS:
    logging.getLogger(logger_name).setLevel(logging.WARNING)


# Add these constants
SEVERITY_MAPPING = {
    "DEBUG": logging.getLevelNamesMapping()["DEBUG"],
    "INFO": logging.getLevelNamesMapping()["INFO"],
    "WARNING": logging.getLevelNamesMapping()["WARNING"],
    "ERROR": logging.getLevelNamesMapping()["ERROR"],
    "CRITICAL": logging.getLevelNamesMapping()["CRITICAL"],
    "ACTIVITY": logging.getLevelNamesMapping()[
        "INFO"
    ],  # Using INFO severity for activity level
    "METRIC": logging.getLevelNamesMapping()[
        "DEBUG"
    ],  # Using DEBUG severity for metric level
    "TRACING": logging.getLevelNamesMapping()[
        "DEBUG"
    ],  # Using DEBUG severity for tracing level
}


class AtlanLoggerAdapter(AtlanObservability[Any]):
    """A custom logger adapter for Atlan that extends AtlanObservability.

    This adapter provides enhanced logging capabilities including:
    - Structured logging with context
    - OpenTelemetry integration
    - Parquet file logging
    - Custom log levels for activities, metrics, and tracing
    - Temporal workflow and activity context integration
    """

    _flush_task_started: ClassVar[bool] = False
    _flush_task: ClassVar[Any] = None
    _initialized: ClassVar[bool] = False

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Reset initialization state for test isolation.

        This method should only be used in tests to allow fresh sink setup
        for each test case.
        """
        cls._initialized = False

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
            data_dir=get_observability_dir(),
            file_name=LOG_FILE_NAME,
        )
        self.logger_name = logger_name
        # Bind the logger name when creating the logger instance
        self.logger = logger

        if AtlanLoggerAdapter._initialized:
            return

        logger.remove()

        # Register custom log level for activity
        if "ACTIVITY" not in logger._core.levels:
            logger.level(
                "ACTIVITY", no=SEVERITY_MAPPING["ACTIVITY"], color="<cyan>", icon="🔵"
            )

        # Register custom log level for metrics
        if "METRIC" not in logger._core.levels:
            logger.level(
                "METRIC", no=SEVERITY_MAPPING["METRIC"], color="<yellow>", icon="📊"
            )

        # Register custom log level for tracing
        if "TRACING" not in logger._core.levels:
            logger.level(
                "TRACING", no=SEVERITY_MAPPING["TRACING"], color="<magenta>", icon="🔍"
            )

        # Colorize the logs only if the log level is DEBUG
        colorize = LOG_LEVEL == "DEBUG"

        def get_log_format(record: Any) -> str:
            """Generate log format string with trace_id and correlation_id for correlation.

            Args:
                record: Loguru record dictionary containing log information.

            Returns:
                Format string for the log message.
            """
            # Build trace_id display string (only trace_id is printed, atlan-* go to OTEL)
            trace_id = record["extra"].get("trace_id", "")
            record["extra"]["_trace_id_str"] = (
                f" trace_id={trace_id}" if trace_id else ""
            )
            record["extra"].setdefault("logger_name", "")

            # Build correlation_id display string from extra (set by the v3
            # CorrelationContextInterceptor bridge or bound directly).
            correlation_id = record["extra"].get("correlation_id", "")
            record["extra"]["_correlation_id_str"] = (
                f" correlation_id={correlation_id}" if correlation_id else ""
            )

            if colorize:
                return (
                    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
                    "<blue>[{level}]</blue>"
                    "<magenta>{extra[_trace_id_str]}</magenta>"
                    "<yellow>{extra[_correlation_id_str]}</yellow> "
                    "<cyan>{extra[logger_name]}</cyan>"
                    " - <level>{message}</level>\n{exception}"
                )
            return (
                "{time:YYYY-MM-DD HH:mm:ss} [{level}]"
                "{extra[_trace_id_str]}{extra[_correlation_id_str]} {extra[logger_name]}"
                " - {message}\n{exception}"
            )

        self.logger.add(
            sys.stderr,
            format=get_log_format,
            level=SEVERITY_MAPPING[LOG_LEVEL],
            colorize=colorize,
        )

        # Add sink for store logging only if store sink is enabled
        if ENABLE_OBSERVABILITY_STORE_SINK:
            self.logger.add(self.parquet_sink, level=SEVERITY_MAPPING[LOG_LEVEL])
            # Start flush task only if Dapr sink is enabled
            if not AtlanLoggerAdapter._flush_task_started:
                try:
                    try:
                        loop = asyncio.get_running_loop()
                        AtlanLoggerAdapter._flush_task = loop.create_task(
                            self._periodic_flush()
                        )
                    except RuntimeError:
                        threading.Thread(
                            target=self._start_asyncio_flush, daemon=True
                        ).start()
                    AtlanLoggerAdapter._flush_task_started = True
                except Exception:
                    logging.error("Failed to start flush task", exc_info=True)

        # OTLP export: build list of processors, wire up only if any are enabled.
        #   ENABLE_OTLP_LOGS: primary exporter (host DaemonSet, central ClickHouse)
        #   ENABLE_OTLP_WORKFLOW_LOGS: secondary exporter (tenant collector, S3/Kafka)
        try:
            otlp_processors = []

            if ENABLE_OTLP_LOGS or _has_remote_otlp_endpoint():
                otlp_processors.append(
                    BatchLogRecordProcessor(
                        OTLPLogExporter(
                            endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
                            timeout=OTEL_EXPORTER_TIMEOUT_SECONDS,
                        ),
                        schedule_delay_millis=OTEL_BATCH_DELAY_MS,
                        max_export_batch_size=OTEL_BATCH_SIZE,
                        max_queue_size=OTEL_QUEUE_SIZE,
                    )
                )
                logging.info(
                    "OTLP primary exporter enabled: %s", OTEL_EXPORTER_OTLP_ENDPOINT
                )

            if ENABLE_OTLP_WORKFLOW_LOGS and OTEL_WORKFLOW_LOGS_ENDPOINT:
                otlp_processors.append(
                    BatchLogRecordProcessor(
                        OTLPLogExporter(
                            endpoint=OTEL_WORKFLOW_LOGS_ENDPOINT,
                            timeout=OTEL_EXPORTER_TIMEOUT_SECONDS,
                        ),
                        schedule_delay_millis=OTEL_BATCH_DELAY_MS,
                        max_export_batch_size=OTEL_BATCH_SIZE,
                        max_queue_size=OTEL_QUEUE_SIZE,
                    )
                )
                logging.info(
                    "OTLP workflow logs exporter enabled: %s",
                    OTEL_WORKFLOW_LOGS_ENDPOINT,
                )

            if otlp_processors:
                self.logger_provider = LoggerProvider(
                    resource=build_otel_resource(
                        extra_attrs={"sdk.version": _SDK_VERSION}
                    )
                )
                for processor in otlp_processors:
                    self.logger_provider.add_log_record_processor(processor)

                self.logger.add(self.otlp_sink, level=SEVERITY_MAPPING[LOG_LEVEL])

        except Exception:
            logging.error("Failed to setup OTLP logging", exc_info=True)

        # Mark initialization complete only after all sinks are successfully added
        AtlanLoggerAdapter._initialized = True

    def process_record(self, record: Any) -> Dict[str, Any]:
        """Process a log record into a standardized dictionary format.

        Args:
            record (Any): Input log record, can be a loguru message or pre-built dict.

        Returns:
            Dict[str, Any]: Standardized dictionary representation of the log record.

        Raises:
            ValueError: If the record format is not supported.
        """
        # Handle loguru message format
        if hasattr(record, "record"):
            return _make_log_record_dict(record)

        # Handle pre-built log record dict
        if isinstance(record, dict):
            return record

        raise ValueError(f"Unsupported record format: {type(record)}")

    def export_record(self, record: Any) -> None:
        """Export a log record to external systems.

        OTLP export is handled exclusively by the otlp_sink; this path is a no-op.
        """
        pass

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
        - Adds correlation context if available
        """
        kwargs["logger_name"] = self.logger_name
        kwargs["app_name"] = APPLICATION_NAME

        # Get request context
        ctx = request_context.get()
        if ctx and "request_id" in ctx:
            kwargs["request_id"] = ctx["request_id"]

        workflow_context = get_workflow_context()

        if (
            workflow_context.get("in_workflow") == "true"
            or workflow_context.get("in_activity") == "true"
        ):
            kwargs.update(workflow_context)

        # Add correlation context (atlan-, temporal., tenant. prefixed keys, trace_id, correlation_id) to kwargs
        corr_ctx = correlation_context.get()
        if corr_ctx:
            # Add trace_id if present (for log format display)
            if "trace_id" in corr_ctx and corr_ctx["trace_id"]:
                kwargs["trace_id"] = str(corr_ctx["trace_id"])
            # Add correlation_id if present (AppWorkflowRun GUID for e2e correlation)
            if "correlation_id" in corr_ctx and corr_ctx["correlation_id"]:
                kwargs["correlation_id"] = str(corr_ctx["correlation_id"])
            # Add atlan-* headers for OTEL
            for key, value in corr_ctx.items():
                if (
                    key.startswith("atlan-")
                    or key.startswith("temporal.")
                    or key.startswith("tenant.")
                ) and value:
                    if isinstance(value, (bool, int, float, str, bytes)):
                        kwargs[key] = value
                    else:
                        kwargs[key] = str(value)

        # Bridge: if legacy correlation_context dict is empty, read from v3
        # CorrelationContext ContextVar (set by the v3 CorrelationContextInterceptor).
        if "correlation_id" not in kwargs:
            from application_sdk.observability.correlation import (
                get_correlation_context,
            )

            v3_ctx = get_correlation_context()
            if v3_ctx and v3_ctx.correlation_id:
                kwargs["correlation_id"] = v3_ctx.correlation_id

        return msg, kwargs

    def debug(self, msg: str, *args: Any, **kwargs: Any):
        """Log a debug level message.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context
        """
        try:
            msg, args = _format_printf_args(msg, args)
            exc_info = kwargs.pop("exc_info", False)
            msg, kwargs = self.process(msg, kwargs)
            bound_logger = self.logger.bind(**kwargs)
            if exc_info:
                bound_logger.opt(exception=exc_info).debug(msg, *args)
            else:
                bound_logger.debug(msg, *args)
        except Exception:
            logging.error("Error in debug logging", exc_info=True)
            self._sync_flush()

    def info(self, msg: str, *args: Any, **kwargs: Any):
        """Log an info level message.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context
        """
        try:
            msg, args = _format_printf_args(msg, args)
            exc_info = kwargs.pop("exc_info", False)
            msg, kwargs = self.process(msg, kwargs)
            bound_logger = self.logger.bind(**kwargs)
            if exc_info:
                bound_logger.opt(exception=exc_info).info(msg, *args)
            else:
                bound_logger.info(msg, *args)
        except Exception:
            logging.error("Error in info logging", exc_info=True)
            self._sync_flush()

    def warning(self, msg: str, *args: Any, **kwargs: Any):
        """Log a warning level message.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context
        """
        try:
            msg, args = _format_printf_args(msg, args)
            exc_info = kwargs.pop("exc_info", False)
            msg, kwargs = self.process(msg, kwargs)
            bound_logger = self.logger.bind(**kwargs)
            if exc_info:
                bound_logger.opt(exception=exc_info).warning(msg, *args)
            else:
                bound_logger.warning(msg, *args)
        except Exception:
            logging.error("Error in warning logging", exc_info=True)
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
            msg, args = _format_printf_args(msg, args)
            exc_info = kwargs.pop("exc_info", False)
            msg, kwargs = self.process(msg, kwargs)
            bound_logger = self.logger.bind(**kwargs)
            if exc_info:
                bound_logger.opt(exception=exc_info).error(msg, *args)
            else:
                bound_logger.error(msg, *args)
            # Force flush on error logs
            self._sync_flush()
        except Exception:
            logging.error("Error in error logging", exc_info=True)
            self._sync_flush()

    def exception(self, msg: str, *args: Any, **kwargs: Any):
        """Log an error level message with the current exception traceback.

        Equivalent to error() with exc_info=True. Matches the interface of
        logging.Logger.exception() so that callers such as the Temporal SDK
        (activity.logger.exception(...)) work correctly.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context
        """
        kwargs.setdefault("exc_info", True)
        self.error(msg, *args, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any):
        """Log a critical level message.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context

        Note: Forces an immediate flush of logs when called.
        """
        try:
            msg, args = _format_printf_args(msg, args)
            exc_info = kwargs.pop("exc_info", False)
            msg, kwargs = self.process(msg, kwargs)
            bound_logger = self.logger.bind(**kwargs)
            if exc_info:
                bound_logger.opt(exception=exc_info).critical(msg, *args)
            else:
                bound_logger.critical(msg, *args)
            # Force flush on critical logs
            self._sync_flush()
        except Exception:
            logging.error("Error in critical logging", exc_info=True)
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
            msg, args = _format_printf_args(msg, args)
            local_kwargs = kwargs.copy()
            local_kwargs["log_type"] = "activity"
            processed_msg, processed_kwargs = self.process(msg, local_kwargs)
            self.logger.bind(**processed_kwargs).log("ACTIVITY", processed_msg, *args)
        except Exception:
            logging.error("Error in activity logging", exc_info=True)
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
            msg, args = _format_printf_args(msg, args)
            local_kwargs = kwargs.copy()
            local_kwargs["log_type"] = "metric"
            processed_msg, processed_kwargs = self.process(msg, local_kwargs)
            self.logger.bind(**processed_kwargs).log("METRIC", processed_msg, *args)
        except Exception:
            logging.error("Error in metric logging", exc_info=True)
            self._sync_flush()

    def _send_to_otel(self, record: Dict[str, Any]):
        """Send log record to OpenTelemetry.

        Args:
            record (Dict[str, Any]): Log record dict to send

        This method:
        - Creates an OpenTelemetry LogRecord
        - Gets the logger from the provider
        - Emits the log record
        """
        try:
            otel_record = self._create_log_record(record)
            otel_logger = self.logger_provider.get_logger(SERVICE_NAME)
            otel_logger.emit(otel_record)
        except Exception:
            logging.error("Error sending log to OpenTelemetry", exc_info=True)

    def _sync_flush(self):
        """Flush the log buffer, dispatching appropriately for the current context.

        Called on error/critical logs to force-flush. Works in three contexts:

        - Async context (running event loop in this thread): schedules a task.
        - Sync context (no running loop): creates a temporary loop to flush.
        - Thread context (loop running in another thread, e.g. Temporal
          activity in ThreadPoolExecutor): skips, periodic flush handles it.
        """
        try:
            try:
                loop = asyncio.get_running_loop()
                # We're in an async context — schedule non-blocking flush
                loop.create_task(self._flush_buffer(force=True))
            except RuntimeError:
                # No running loop in this thread. Safe to create one.
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self._flush_buffer(force=True))
                finally:
                    loop.close()
        except Exception:
            logging.error("Error during sync flush", exc_info=True)

    def tracing(self, msg: str, *args: Any, **kwargs: Any):
        """Log a trace-specific message with trace context.

        Args:
            msg (str): Message to log
            *args: Additional positional arguments
            **kwargs: Additional keyword arguments for context

        This method adds trace-specific context to the log message.
        """
        msg, args = _format_printf_args(msg, args)
        local_kwargs = kwargs.copy()
        local_kwargs["log_type"] = "trace"
        processed_msg, processed_kwargs = self.process(msg, local_kwargs)
        self.logger.bind(**processed_kwargs).log("TRACING", processed_msg, *args)

    async def parquet_sink(self, message: Any):
        """Process log message and store in parquet format.

        Args:
            message (Any): Log message to process and store

        This method:
        - Builds a log record dict from the message
        - Adds the record to the buffer for parquet storage
        """
        try:
            log_record = _make_log_record_dict(message)
            self.add_record(log_record)
        except Exception:
            logging.error("Error buffering log", exc_info=True)

    def otlp_sink(self, message: Any):
        """Process log message and emit to OTLP.

        Args:
            message (Any): Log message to process and emit

        This method:
        - Builds a log record dict from the message
        - Sends the record to OpenTelemetry
        """
        try:
            log_record = _make_log_record_dict(message)
            self._send_to_otel(log_record)
        except Exception:
            logging.error("Error processing log record", exc_info=True)

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
