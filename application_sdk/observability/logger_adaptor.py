import asyncio
import logging
import sys
import threading
import traceback as tb_module
from typing import Any, ClassVar

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

# SDK-side allowlist that gates which kwargs reach OTLP.  When a logger is called
# with structured kwargs (e.g. ``_log().info("Downloaded", storage_path=key)``),
# loguru places the kwargs on ``record["extra"]`` rather than in the message
# string.  ``_build_extra_dict`` filters that dict through this set before it is
# copied into the emitted OTLP LogRecord's ``attributes`` map — keys not listed
# here are dropped and never reach the exporter.
_KNOWN_EXTRA_KEYS = frozenset(
    {
        # ── HTTP request/response ────────────────────────────────────────
        "client_host",
        "duration_ms",
        "method",
        "path",
        "request_id",
        "status_code",
        "url",
        # ── Temporal workflow / activity context ─────────────────────────
        # Auto-injected by `process()` + `get_workflow_context()` on every log
        # emitted inside a workflow/activity (see logger_adaptor.process and
        # observability/utils.get_workflow_context).
        "in_workflow",
        "in_activity",
        "workflow_id",
        "workflow_run_id",
        # Backwards-compat alias for ``workflow_run_id``.
        "run_id",
        "workflow_type",
        "namespace",
        "task_queue",
        "attempt",
        "activity_id",
        "activity_type",
        # Parent identity — only emitted on child workflows (workflow.info().parent)
        "parent_workflow_id",
        "parent_run_id",
        # Activity timeout fields — emitted by app authors logging activity
        # configuration; not auto-injected.
        "schedule_to_close_timeout",
        "start_to_close_timeout",
        "schedule_to_start_timeout",
        "heartbeat_timeout",
        # ── Outcome / error ──────────────────────────────────────────────
        "status",
        "error_type",
        "error_class",
        "error_message",
        "stack_trace",
        # ── Misc SDK ─────────────────────────────────────────────────────
        "log_type",
        "app_name",
        "trace_id",
        "span_id",
        "correlation_id",
        # ── ObjectStore operations ───────────────────────────────────────
        "storage_op",
        "store_path",
        "outcome",
        "elapsed_ms",
        "size_bytes",
        "throughput_mibps",
        # ── FileReference transfers ──────────────────────────────────────
        "storage_path",
        "local_path",
        "file_size_bytes",
        "bytes_uploaded",
        "bytes_downloaded",
        "bytes_transferred_before_failure",
        "sha256",
        "tier",
        "file_count",
        "files_skipped",
        "files_downloaded",
        "chunk_size_bytes",
        "chunks_total",
        "chunks_completed",
        "is_cache_hit",
        "reused_local_path",
        "dedup_key",
        "chunk_offset",
        "chunk_length",
    }
)


_PREFIXES_PASSTHROUGH = (
    "atlan.",  # SDK convention: atlan.correlation_id and similar dotted keys
    "exception.",  # OTel semconv: exception.type/message/stacktrace
    "failure.",  # SDK convention: failure.category/audience/code from AppError
    "otel.",  # OTel semconv: otel.status_code
    "temporal.",  # SDK convention: temporal.workflow.id, etc.
    "tenant.",
    "workflow_run.",  # AE convention: workflow_run.terminated / workflow_run.node
    # events emitted from AutomationEngineWorkflow's finally block,
    # carrying typed FailureDetails (category, code, audience,
    # retryable, evidence) projected from the cause chain.
)


def _build_extra_dict(
    record_extra: dict[str, Any], exception: Any = None
) -> dict[str, Any]:
    """Build a dict of structured log extra fields from a loguru record's extra dict."""
    extra: dict[str, Any] = {}
    for k, v in record_extra.items():
        if k == "logger_name":
            continue
        if k in _KNOWN_EXTRA_KEYS:
            extra[k] = _normalize_log_extra_value(k, v)
        elif k.startswith(_PREFIXES_PASSTHROUGH) and v is not None:
            extra[k] = v if isinstance(v, (bool, int, float, str, bytes)) else str(v)
    for key, value in _extract_exception_attributes(exception).items():
        extra[key] = value
    return extra


def _make_log_record_dict(message: Any) -> dict[str, Any]:
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


def _extract_exception_attributes(exception: Any) -> dict[str, str]:
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

    attrs: dict[str, str] = {"exception.type": type_name}
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


def _format_printf_args(msg: str, args: tuple[Any, ...]) -> tuple[str, tuple[Any, ...]]:
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
        from urllib.parse import (  # noqa: PLC0415 — stdlib urllib.parse; lazy use only on URL config
            urlparse,
        )

        host = urlparse(ep).hostname or ""
        return host not in ("", "localhost", "127.0.0.1", "::1")
    except Exception:
        logging.debug("OTEL endpoint check failed, treating as local", exc_info=True)
        return False


#: Built-in attributes set on every ``logging.LogRecord``. Computed once at
#: import time from a dummy record so this stays correct across Python
#: versions (``taskName`` was added in 3.12, future versions may add more).
#: Anything on a record's ``__dict__`` outside this set came from a
#: caller-supplied ``extra={...}`` and needs to flow to loguru.
_LOGRECORD_RESERVED_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord(
        name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__
)


class InterceptHandler(logging.Handler):
    """Bridge Python's stdlib logging into loguru, preserving ``extra={...}``.

    Without forwarding the extras, callers like ``storage/ops.py`` that use
    stdlib ``logging.getLogger`` and emit ``logger.log(..., extra={"outcome":
    "success", ...})`` would have their structured fields silently dropped
    on the way to the OTLP exporter — ``outcome``, ``elapsed_ms``, etc.
    would never reach the exporter.
    """

    def emit(self, record: logging.LogRecord) -> None:
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

        # Forward caller-supplied ``extra={...}``. Python's stdlib spreads
        # ``extra`` directly into ``record.__dict__``; the delta vs. a blank
        # LogRecord is exactly what the caller passed.
        logger_extras: dict[str, object] = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _LOGRECORD_RESERVED_ATTRS
        }
        # SDK convention: ``logger_name`` always tracks ``record.name``.
        # Set last so callers can't shadow it via ``extra``.
        logger_extras["logger_name"] = record.name

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


class _LazyLoggerProxy:
    """Returned by AtlanLoggerAdapter.opt(); context is pre-bound, loguru handles lazy eval."""

    __slots__ = ("_logger",)

    def __init__(self, bound_opt_logger: Any) -> None:
        self._logger = bound_opt_logger

    def debug(self, msg: str, **kwargs: Any) -> None:
        try:
            self._logger.debug(msg, **kwargs)
        except Exception:
            logging.error("Error in lazy debug logging", exc_info=True)

    def info(self, msg: str, **kwargs: Any) -> None:
        try:
            self._logger.info(msg, **kwargs)
        except Exception:
            logging.error("Error in lazy info logging", exc_info=True)

    def warning(self, msg: str, **kwargs: Any) -> None:
        try:
            self._logger.warning(msg, **kwargs)
        except Exception:
            logging.error("Error in lazy warning logging", exc_info=True)

    def error(self, msg: str, **kwargs: Any) -> None:
        try:
            self._logger.error(msg, **kwargs)
        except Exception:
            logging.error("Error in lazy error logging", exc_info=True)

    def critical(self, msg: str, **kwargs: Any) -> None:
        try:
            self._logger.critical(msg, **kwargs)
        except Exception:
            logging.error("Error in lazy critical logging", exc_info=True)


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

        Public test hook: any new ClassVar or module-level cache associated
        with logger initialization MUST be reset here so test isolation
        stays correct. Currently resets ``_initialized``, ``_flush_task_started``,
        and clears the module-level ``_logger_instances`` cache.

        This method should only be used in tests to allow fresh sink setup
        for each test case.
        """
        cls._initialized = False
        cls._flush_task_started = False
        _logger_instances.clear()

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

        # Split sinks by severity so cloud log collectors (GCP Cloud Logging,
        # AWS CloudWatch Logs Insights, etc.) that infer severity from the file
        # descriptor classify benign records as INFO instead of ERROR. Records
        # at WARNING and below go to stdout; ERROR/CRITICAL/exception records
        # stay on stderr. Matches uvicorn/gunicorn conventions.
        _ERROR_LEVEL_NO = SEVERITY_MAPPING["ERROR"]
        self.logger.add(
            sys.stdout,
            format=get_log_format,
            level=SEVERITY_MAPPING[LOG_LEVEL],
            colorize=colorize,
            filter=lambda record: record["level"].no < _ERROR_LEVEL_NO,
        )
        self.logger.add(
            sys.stderr,
            format=get_log_format,
            level=max(SEVERITY_MAPPING[LOG_LEVEL], _ERROR_LEVEL_NO),
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
                        self._spawn_flush_thread()
                    AtlanLoggerAdapter._flush_task_started = True
                except Exception:
                    logging.error("Failed to start flush task", exc_info=True)

        # OTLP log export — primary exporter to OTEL_EXPORTER_OTLP_ENDPOINT,
        # plus an optional secondary exporter to OTEL_WORKFLOW_LOGS_ENDPOINT
        # for archival pipelines (e.g. an OTel collector that writes to S3).
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
                logging.info("OTLP exporter enabled: %s", OTEL_EXPORTER_OTLP_ENDPOINT)

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

    def process_record(self, record: Any) -> dict[str, Any]:
        """Process a log record into a standardized dictionary format.

        Args:
            record (Any): Input log record, can be a loguru message or pre-built dict.

        Returns:
            dict[str, Any]: Standardized dictionary representation of the log record.

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
        attributes: dict[str, Any] = {
            "code.filepath": record["file"],
            "code.function": record["function"],
            "code.lineno": record["line"],
            "level": record["level"],
        }

        # Add error code if present in extra
        if "extra" in record and "error_code" in record["extra"]:
            attributes["error.code"] = record["extra"]["error_code"]

        # Skip None — str(None) leaks the literal "None" string downstream.
        if "extra" in record:
            for key, value in record["extra"].items():
                if key == "error_code" or value is None:
                    continue
                if isinstance(value, (bool, int, float, str, bytes)):
                    attributes[key] = value
                else:
                    attributes[key] = str(value)

        return LogRecord(
            timestamp=int(record["timestamp"] * 1e9),
            observed_timestamp=int(record["timestamp"] * 1e9),
            trace_id=0,
            span_id=0,
            trace_flags=TraceFlags(0),
            severity_text=record["level"],
            severity_number=severity_number,
            body=record["message"],
            attributes=attributes,
        )

    def _spawn_flush_thread(self) -> None:
        """Spawn a daemon thread to run the asyncio flush loop.

        Extracted for testability — lets tests assert this specific call without
        patching threading.Thread globally (which would also capture OTel internals).
        """
        threading.Thread(target=self._start_asyncio_flush, daemon=True).start()

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

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """Process log message with temporal and request context.

        Args:
            msg (Any): Original log message
            kwargs (dict[str, Any]): Additional logging parameters

        Returns:
            tuple[Any, dict[str, Any]]: Processed message and updated kwargs with context

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
            if corr_ctx.get("trace_id"):
                kwargs["trace_id"] = str(corr_ctx["trace_id"])
            # Add correlation_id if present (AppWorkflowRun GUID for e2e correlation)
            if corr_ctx.get("correlation_id"):
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
            from application_sdk.observability.correlation import (  # noqa: PLC0415 — circular: observability is imported transitively by many modules; lifting risks circles
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

    def opt(
        self, *, lazy: bool = False, **loguru_opt_kwargs: Any
    ) -> "_LazyLoggerProxy":
        """Return a proxy with loguru opt() flags applied and context pre-bound.

        Primary use case is lazy=True for performance-critical debug paths where
        the argument expression is expensive to compute:

            self.logger.opt(lazy=True).debug("record {data}", data=lambda: json.dumps(record))

        The lambda is evaluated only when DEBUG is enabled. Use loguru's {key}
        format (not %-style) when passing lazy kwargs.
        """
        _, ctx_kwargs = self.process("", {})
        bound = self.logger.bind(**ctx_kwargs).opt(lazy=lazy, **loguru_opt_kwargs)
        return _LazyLoggerProxy(bound)

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

    def _send_to_otel(self, record: dict[str, Any]):
        """Send log record to OpenTelemetry.

        Args:
            record (dict[str, Any]): Log record dict to send

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
_logger_instances: dict[str, AtlanLoggerAdapter] = {}


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
