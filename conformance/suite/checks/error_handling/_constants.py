"""Shared constants and compiled regexes for the E-series checker."""

from __future__ import annotations

import re

SERIES = "E"

# E012 — builtin exception types that should be replaced by typed AppError leaves
BUILTIN_RAISES: frozenset[str] = frozenset(
    {
        "ValueError",
        "RuntimeError",
        "Exception",
        "TypeError",
        "NotImplementedError",
        "OSError",
        "KeyError",
        "LookupError",
    }
)

# E013 — legacy AtlanError subclass names (all from application_sdk.common.error_codes)
LEGACY_ATLAN_ERRORS: frozenset[str] = frozenset(
    {
        "ClientError",
        "ApiError",
        "OrchestratorError",
        "WorkflowError",
        "IOError",
        "CommonError",
        "DocGenError",
        "ActivityError",
        "AtlanError",
    }
)
_ATLAN_LEGACY_MODULE = "application_sdk.common.error_codes"

# E018 — bare parent AppError leaf classes (application_sdk/errors/leaves.py)
LEAF_CLASSES: frozenset[str] = frozenset(
    {
        "CancelledError",
        "AppTimeoutError",
        "RateLimitedError",
        "AuthError",
        "AppPermissionDeniedError",
        "NotFoundError",
        "AlreadyExistsError",
        "InvalidInputError",
        "PreconditionError",
        "DependencyUnavailableError",
        "ResourceExhaustedError",
        "DataIntegrityError",
        "InternalError",
        "UnimplementedError",
    }
)

# E012 carve-outs: builtin raises acceptable in these method names (stdlib interop)
INTEROP_METHODS: frozenset[str] = frozenset({"__post_init__", "__init__"})
# E012 carve-outs: acceptable in Pydantic validator-decorated functions
INTEROP_DECORATORS: frozenset[str] = frozenset({"field_validator", "validator"})
# E012 escalation: note when inside Temporal activity body
ACTIVITY_DECORATORS: frozenset[str] = frozenset({"task", "defn"})

# Directories excluded from discovery
EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "tests",
        "test",
        "conformance",
        "docs",
        ".tox",
        "site-packages",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "htmlcov",
    }
)

_SECRET_SUFFIXES: tuple[str, ...] = ("_secret", "_password", "_token")
_BROAD_EXCEPT_TYPES: frozenset[str] = frozenset({"Exception", "BaseException"})
_OPTIONAL_IMPORT_TYPES: frozenset[str] = frozenset(
    {"ImportError", "ModuleNotFoundError"}
)
_LOG_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception"}
)

# Directive regex: "# conformance: ignore[E001,E002] some reason"
_SUPPRESS_RE = re.compile(
    r"^#\s*conformance\s*:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)",
    re.IGNORECASE,
)

# A noqa comment is accepted ONLY when it names at least one mapped code AND
# supplies non-empty justification text after a visual separator (—, –, or -).
# Bare "# noqa" and "# noqa: CODE" with no justification are rejected.
_NOQA_RE = re.compile(
    r"^#\s*noqa\s*:\s*([A-Z][A-Z0-9]*(?:\s*,\s*[A-Z][A-Z0-9]*)*)\s*[—–\-]+\s*(.+)$",
    re.IGNORECASE,
)
_NOQA_TO_RULES: dict[str, frozenset[str]] = {
    "S110": frozenset({"E001", "E002"}),  # try-except-pass (bare or typed)
    "BLE001": frozenset({"E004"}),  # blind/broad exception catch
    "S112": frozenset({"E014"}),  # try-except-continue (loop swallow)
}
