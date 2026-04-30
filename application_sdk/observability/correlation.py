"""Correlation context for distributed tracing.

Provides a ContextVar-based correlation context that propagates
correlation IDs across workflow and activity boundaries.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class CorrelationContext:
    """Holds the current correlation ID for a request or workflow run."""

    correlation_id: str = ""

    @staticmethod
    def from_temporal_memo(memo: dict) -> CorrelationContext | None:
        """Restore a CorrelationContext from a Temporal workflow memo.

        Used by the correlation interceptor to restore context after
        continue-as-new.

        Args:
            memo: Temporal workflow memo dict.

        Returns:
            CorrelationContext if correlation_id is present in memo, else None.
        """
        corr_id = memo.get("correlation_id", "")
        if corr_id:
            return CorrelationContext(correlation_id=str(corr_id))
        return None

    def to_temporal_memo(self) -> dict[str, str]:
        """Serialize to a Temporal workflow memo dict.

        Used when starting a top-level workflow so the correlation ID
        is available to the inbound interceptor.

        Returns:
            Dict suitable for use as Temporal workflow memo.
        """
        return {"correlation_id": self.correlation_id}


_correlation_ctx: ContextVar[CorrelationContext | None] = ContextVar(
    "correlation_ctx", default=None
)


def get_correlation_context() -> CorrelationContext | None:
    """Get the current correlation context.

    Returns:
        Current CorrelationContext, or None if not set.
    """
    return _correlation_ctx.get()


def set_correlation_context(ctx: CorrelationContext) -> None:
    """Set the current correlation context.

    Args:
        ctx: The correlation context to set.
    """
    _correlation_ctx.set(ctx)
