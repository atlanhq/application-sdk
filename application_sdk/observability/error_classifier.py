"""Classify exceptions into App Vitals error categories.

Used by the AppVitalsInterceptor to stamp ``error.type`` on every
failure event. The classification drives downstream computed metrics
like ``app.error_rate.<category>``.

Categories align with the App Vitals RFC (06-metrics-catalog.md §1.2):
  transient — retryable network/rate-limit/temporary failures
  config    — bad credentials, permissions, connection strings
  oom       — memory limit exceeded, OOMKilled
  upstream  — source/dependency system returned an error
  app_bug   — unhandled logic error in app code
  timeout   — hit Temporal or asyncio timeout
  unknown   — unclassifiable (should trend toward zero)
"""

from __future__ import annotations

import asyncio


def classify_error(exc: BaseException) -> str:
    """Return an error category string for the given exception.

    The classifier checks exception type and message heuristics.
    It is intentionally simple — better to be fast and wrong on edge
    cases than to import heavy dependencies.
    """
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__ or ""
    msg = str(exc).lower()

    # --- timeout ---
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if "timeout" in exc_type.lower() or "timed out" in msg:
        return "timeout"
    if "cancelled" in exc_type.lower() and "deadline" in msg:
        return "timeout"

    # --- oom ---
    if isinstance(exc, MemoryError):
        return "oom"
    if "oomkilled" in msg or "out of memory" in msg or "oom" in exc_type.lower():
        return "oom"
    if "memory" in msg and ("killed" in msg or "exceeded" in msg or "limit" in msg):
        return "oom"

    # --- config ---
    if isinstance(exc, PermissionError):
        return "config"
    if any(
        kw in msg
        for kw in (
            "credential",
            "authentication",
            "unauthorized",
            "403",
            "permission denied",
            "access denied",
            "invalid connection",
            "connection string",
            "login failed",
        )
    ):
        return "config"
    if "auth" in exc_type.lower() and "error" in exc_type.lower():
        return "config"

    # --- transient ---
    if isinstance(exc, ConnectionError):
        return "transient"
    if any(
        kw in msg
        for kw in (
            "connection refused",
            "connection reset",
            "connection aborted",
            "rate limit",
            "429",
            "503",
            "retry",
            "temporary",
            "unavailable",
            "throttl",
            "too many requests",
        )
    ):
        return "transient"
    if "http" in exc_module.lower() and ("429" in msg or "503" in msg):
        return "transient"

    # --- upstream ---
    if any(
        kw in msg
        for kw in (
            "500",
            "502",
            "504",
            "bad gateway",
            "internal server error",
            "service unavailable",
            "upstream",
            "dependency",
            "source system",
        )
    ):
        return "upstream"
    if "http" in exc_module.lower() and any(
        code in msg for code in ("500", "502", "504")
    ):
        return "upstream"

    # --- app_bug ---
    if isinstance(exc, (AssertionError, TypeError, ValueError, KeyError, IndexError)):
        return "app_bug"
    if isinstance(exc, (AttributeError, NotImplementedError)):
        return "app_bug"

    return "unknown"
