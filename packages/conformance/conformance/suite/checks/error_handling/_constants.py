"""Shared constants for the E-series checker.

The directive grammar (``# conformance: ignore[...]`` / ``# noqa``) and the
Python-source discovery walk are series-agnostic and live in
``conformance.suite.checks._ast_common``; only E-specific constants live here.
"""

from __future__ import annotations

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
        "SourceUnavailableError",
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
# E012 carve-outs: Pydantic validator decorators where only the builtins Pydantic
# converts into a ValidationError are exempt. Under Pydantic v2 a TypeError or
# RuntimeError raised in a model validator propagates unconverted.
VALIDATOR_DECORATOR_BUILTINS: dict[str, frozenset[str]] = {
    "model_validator": frozenset({"ValueError"}),
    "root_validator": frozenset({"ValueError"}),
}
# E012 carve-outs: mapping dunders whose protocol names KeyError as the builtin
# a caller catches (``Mapping.get`` and ``in`` catch KeyError from
# ``__getitem__``). Only KeyError is exempt, and only in a method defined in a
# class body; any other builtin still reports. IndexError, AttributeError and
# StopIteration need no entry: E012 never reports them (see BUILTIN_RAISES).
PROTOCOL_DUNDER_BUILTINS: dict[str, frozenset[str]] = {
    "__getitem__": frozenset({"KeyError"}),
    "__delitem__": frozenset({"KeyError"}),
    "__missing__": frozenset({"KeyError"}),
}
# E012 scope: repo-root directories holding build / dev tooling that never runs
# inside the app runtime, so a raise there never reaches the Automation Engine
# error envelope. Anchored at the repo root: ``app/tools/`` is app code.
E012_TOOLING_ROOTS: frozenset[str] = frozenset({"tools", "contract"})
# E012 escalation: note when inside Temporal activity body
ACTIVITY_DECORATORS: frozenset[str] = frozenset({"task", "defn"})

_SECRET_SUFFIXES: tuple[str, ...] = ("_secret", "_password", "_token")
_BROAD_EXCEPT_TYPES: frozenset[str] = frozenset({"Exception", "BaseException"})
_OPTIONAL_IMPORT_TYPES: frozenset[str] = frozenset(
    {"ImportError", "ModuleNotFoundError"}
)
_LOG_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception"}
)
