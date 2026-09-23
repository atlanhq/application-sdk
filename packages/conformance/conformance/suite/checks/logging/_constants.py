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

# L010 — name *suffixes* that suggest a variable holds a credential *value*.
# The matcher (``_helpers._is_credential_value_name``) is ``str.endswith`` only:
# a variable or kwarg whose lower-cased name *ends with* one of these (and does
# NOT end with a ``CREDENTIAL_LABEL_SUFFIXES`` entry) is treated as a value.  A
# value word in the *middle* of a name never matches — ``aws_secret_access_key``
# ends in ``access_key``, so ``"secret"`` alone does not catch it; the compound
# key forms must be listed as suffixes in their own right.
#
# Already covered by a shorter suffix (kept out of the tuple to avoid
# redundancy, pinned by tests): ``session_token`` / ``refresh_token`` via
# ``"token"``, ``client_secret`` via ``"secret"``.
#
# Deliberately NOT suffixes (ambiguous — this rule is BLOCK tier, scope=both,
# so a false positive reds every consumer build and the SDK's own gate):
#
# * bare ``access_key`` — in S3/MinIO-style configs it holds the public key
#   *ID* (``application_sdk/storage/cloud.py`` maps ``username=access_key``).
#   ``access_key_id`` is exempt anyway via the ``_id`` label suffix.  S002's
#   env-var predicate (``checks/security/_secret_names.py``) does flag bare
#   ``access_key`` — a deliberate divergence for that surface.
# * bare ``secret_key`` — means "the key a secret is stored under" as often as
#   "the secret half of a key pair": the SDK's Dapr credential vault logs
#   ``secret_key`` as the secret-store *lookup key* (a reference, not a value).
#   The trade-off: MinIO-style bare ``secret_key`` / ``base_secret_key`` values
#   escape L010; none is logged in the SDK or the reference apps today.  The
#   unambiguous AWS forms are listed below instead.
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
    # AWS secret keys — ``aws_secret_access_key``, ``secret_access_key``,
    # ``aws_secret_key`` (the tail ``access_key`` / ``secret_key`` alone is ambiguous).
    "secret_access_key",
    "aws_secret_key",
    # Unlocks an encrypted private key — ``private_key_passphrase``.
    "passphrase",
    # DSNs routinely embed the secret (ODBC ``PWD=``, Azure ``AccountKey=``,
    # ``user:password@host`` URLs), so the whole string is a credential value.
    "connection_string",
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
# Each entry: (module_root, call_attr, factory_type)
# module_root="" means a bare function call with no attribute receiver.
# Used by _detect_factory() in __init__.py for the assignment-based detection branch.
FACTORY_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("logging", "getLogger", "stdlib"),
    ("structlog", "get_logger", "structlog"),
    ("", "get_logger", "sdk_adapter"),  # SDK canonical bare call
)
