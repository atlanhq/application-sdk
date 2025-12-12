"""Shared context variables for observability.

This module contains ContextVar definitions that are shared across
multiple observability modules to avoid circular imports.
"""

from contextvars import ContextVar
from typing import Any, Dict

# Context variable for request-scoped data (e.g., request_id from HTTP middleware)
request_context: ContextVar[Dict[str, Any] | None] = ContextVar(
    "request_context", default=None
)

# Context variable for correlation context (atlan- prefixed headers for distributed tracing)
correlation_context: ContextVar[Dict[str, Any] | None] = ContextVar(
    "correlation_context", default=None
)
