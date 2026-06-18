"""Shared constants for the L-series logging checker.

The directive grammar and Python-source discovery walk are series-agnostic and
live in ``conformance.suite.checks._ast_common``; only L-specific constants
live here.
"""

from __future__ import annotations

SERIES = "L"

# L012 — stdlib LogRecord attributes that raise KeyError inside makeRecord() when
# supplied as keys in ``extra={}``.  Source: CPython Lib/logging/__init__.py.
STDLIB_LOG_RECORD_RESERVED: frozenset[str] = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "process",
        "processName",
        "exc_info",
        "exc_text",
        "stack_info",
        "message",
        "asctime",
    }
)

# L013 — kwargs accepted by stdlib logger methods; any other kwarg → TypeError.
STDLIB_LOG_KWARGS_ALLOWED: frozenset[str] = frozenset(
    {"exc_info", "extra", "stack_info", "stacklevel"}
)

# L010 — substrings that suggest a variable holds a credential *value*.
# A variable or kwarg whose name *ends with* one of these (and does NOT end with
# ``_name``, ``_type``, ``_id``, ``_label``) is treated as a value, not a label.
CREDENTIAL_VALUE_SUFFIXES: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "bearer",
    "private_key",
    "auth_token",
    "access_token",
)

# Acceptable suffixes for credential-named variables (labels, not values)
CREDENTIAL_LABEL_SUFFIXES: tuple[str, ...] = (
    "_name",
    "_type",
    "_id",
    "_label",
    "_key_name",
)

# Logger variable names recognised as logger instances by the checker
LOGGER_NAMES: frozenset[str] = frozenset({"logger", "log", "_logger", "_log"})

# All log-method names (universal set)
LOG_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception"}
)

# Methods that accept exc_info and sit inside except blocks (L004)
LOG_METHODS_WITH_TRACEBACK: frozenset[str] = frozenset({"warning", "error"})

# Adapter file markers — a file that *defines* one of these is the logging
# adapter itself.  It is exempt from L017 (the .exception() shim) and L018
# (the factory-call line in the adapter module).
ADAPTER_MARKERS: frozenset[str] = frozenset({"AtlanLoggerAdapter", "get_logger"})

# L008 — attribute names on call targets that indicate expensive serialisation
EXPENSIVE_CALL_ATTRS: frozenset[str] = frozenset(
    {"to_dict", "model_dump", "model_dump_json", "dict", "json"}
)
# L008 — bare function names that indicate expensive serialisation
EXPENSIVE_CALL_NAMES: frozenset[str] = frozenset({"repr", "str"})

# logger factory patterns used for L002 cross-file detection
# Each entry is a (module_root, call_attr) tuple as it appears in AST:
#   logging.getLogger(...)  → module_root="logging", call_attr="getLogger"
#   structlog.get_logger(...)  → "structlog", "get_logger"
#   get_logger(...)  → "", "get_logger"   (bare name call, SDK canonical)
FACTORY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("logging", "getLogger"),
    ("structlog", "get_logger"),
    ("", "get_logger"),  # SDK canonical — also matches structlog.get_logger already
)
