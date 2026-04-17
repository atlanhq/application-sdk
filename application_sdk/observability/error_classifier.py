"""Error classification for App Vitals.

Maps exceptions to a fixed set of error_type values so that failure
breakdowns can be grouped in dashboards and alerts. Also extracts the
exception cause chain and a retriable-heuristic so downstream debugging
tools have structured context without reading stack traces manually.

The classifier is deliberately conservative: if it can't confidently
classify an error, it returns "internal" rather than guessing.
"""

from __future__ import annotations

import asyncio


def classify_error(exc: BaseException) -> str:
    """Classify an exception into an App Vitals error_type.

    If the exception is a Temporal ``ActivityError`` or ``ChildWorkflowError``
    wrapper, we unwrap to the original cause before classifying, so that a
    workflow observing a wrapped ``TimeoutError`` still classifies as "timeout".

    Args:
        exc: The exception to classify.

    Returns:
        One of: "timeout", "oom", "upstream", "config", "cancelled", "internal".
    """
    # Unwrap Temporal wrappers to get to the real cause. The wrapper types are
    # string-matched (not isinstance) to avoid importing from temporalio here.
    # ApplicationError stores the original Python exception class name in
    # `.type` (the actual exception is lost across the wire), so if we end up
    # stuck at an ApplicationError with no deeper cause, we use `.type` as a
    # synthetic class-name hint for downstream matching.
    current = exc
    original_type_hint: str | None = None
    for _ in range(5):  # bounded depth
        cls_name = type(current).__name__
        if cls_name in ("ActivityError", "ChildWorkflowError", "ApplicationError"):
            if cls_name == "ApplicationError":
                # Keep the FIRST (outermost) hint — it represents the actual
                # error the workflow observed. Inner ApplicationErrors are the
                # cause chain, not the raised error.
                if original_type_hint is None:
                    app_err_type = getattr(current, "type", None)
                    if app_err_type:
                        original_type_hint = str(app_err_type)
            inner = getattr(current, "cause", None) or current.__cause__
            if inner is None or inner is current:
                break
            current = inner
            continue
        break
    exc = current

    exc_type = type(exc).__name__
    msg = str(exc).lower()
    # Merge in the ApplicationError `.type` hint (if we unwrapped to one)
    effective_type_names = {exc_type}
    if original_type_hint:
        effective_type_names.add(original_type_hint)

    # Timeout
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if "TimeoutError" in effective_type_names:
        return "timeout"
    if exc_type in ("ActivityError", "ChildWorkflowError") and "timeout" in msg:
        return "timeout"
    if "timed out" in msg or "deadline exceeded" in msg:
        return "timeout"

    # OOM / Resource exhaustion
    if isinstance(exc, MemoryError):
        return "oom"
    if "MemoryError" in effective_type_names:
        return "oom"
    if (
        "oomkilled" in msg
        or "out of memory" in msg
        or ("memory" in msg and "exceeded" in msg)
    ):
        return "oom"

    # Cancellation
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        return "cancelled"
    if effective_type_names & {"CancelledError", "KeyboardInterrupt"}:
        return "cancelled"
    if "cancelled" in msg or "canceled" in msg:
        return "cancelled"

    # Upstream / connectivity
    if isinstance(exc, (ConnectionError, ConnectionRefusedError, ConnectionResetError)):
        return "upstream"
    upstream_types = {
        "AuthenticationError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "SSLError",
        "HTTPError",
    }
    if effective_type_names & upstream_types:
        return "upstream"
    if "connection refused" in msg or "connection reset" in msg:
        return "upstream"
    if "authentication" in msg or "unauthorized" in msg or "403" in msg or "401" in msg:
        return "upstream"

    # Config / validation
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "config"
    config_types = {
        "ValueError",
        "KeyError",
        "TypeError",
        "ConfigurationError",
        "ValidationError",
        "SchemaError",
    }
    if effective_type_names & config_types:
        return "config"
    if (
        "invalid config" in msg
        or "missing required" in msg
        or "not found in config" in msg
    ):
        return "config"

    # Default
    return "internal"


# Error types that never benefit from retry (fix requires code/config change)
_NON_RETRIABLE_TYPES = frozenset({"config", "cancelled"})


def extract_cause_chain(exc: BaseException, limit: int = 5) -> list[str]:
    """Walk the exception cause chain and return a list of cause descriptions.

    Follows ``__cause__`` (``raise X from Y``) and ``__context__`` (implicit
    chaining) up to ``limit`` levels. Each entry is ``"<ExceptionClass>: <message>"``.

    Returns an empty list if there's no cause chain.
    """
    causes: list[str] = []
    current = exc.__cause__ or exc.__context__
    seen: set[int] = {id(exc)}

    while current is not None and len(causes) < limit:
        if id(current) in seen:
            break
        seen.add(id(current))
        causes.append(f"{type(current).__name__}: {str(current)[:200]}")
        current = current.__cause__ or current.__context__

    return causes


def is_retriable(exc: BaseException, error_type: str | None = None) -> bool:
    """Heuristic for whether this error is worth retrying.

    - ``config`` / ``cancelled`` errors don't benefit from retry — they need
      human or code intervention.
    - ``timeout`` / ``upstream`` / ``oom`` / ``internal`` are retriable by
      default (retry policy may still cap attempts).

    Respects an explicit ``non_retryable`` flag on the exception if present
    (Temporal's ApplicationError pattern).

    Args:
        exc: The exception to evaluate.
        error_type: Optional pre-computed error_type from classify_error().
                    If None, classifies internally.
    """
    # Explicit Temporal ApplicationError non_retryable flag
    if getattr(exc, "non_retryable", False):
        return False

    if error_type is None:
        error_type = classify_error(exc)

    return error_type not in _NON_RETRIABLE_TYPES
