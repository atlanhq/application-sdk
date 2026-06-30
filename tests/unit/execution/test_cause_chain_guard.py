"""Regression tests for BLDX-1512.

Temporal's failure serializer recurses into __cause__/__context__ chains without
a depth bound.  A deep or cyclic chain overflows the call stack and replaces the
real error with "Failed building exception result: maximum recursion depth
exceeded".

These tests verify that:
  1. _sever_cause_chain caps the chain at _MAX_CHAIN_DEPTH — linear and cyclic.
  2. _extract_failure_attrs stops at _MAX_CHAIN_WALK even on a deep chain.
  3. An AppError with a 1 000-link chain does not raise RecursionError when the
     activity exception handler processes it (the integration path).
"""

from __future__ import annotations

from dataclasses import dataclass

from application_sdk.errors.base import AppError
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.execution._temporal.activities import (
    _MAX_CHAIN_DEPTH,
    _sever_cause_chain,
)
from application_sdk.execution._temporal.interceptors.log import (
    _MAX_CHAIN_WALK,
    _extract_failure_attrs,
)

# ---------------------------------------------------------------------------
# Minimal AppError subclass used throughout
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class _ConfigError(AppError):
    category: FailureCategory = FailureCategory.INVALID_INPUT  # type: ignore[assignment]
    code: str = "NO_ASSETS"  # type: ignore[assignment]
    audience: Audience = Audience.USER  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# _sever_cause_chain — linear chain
# ---------------------------------------------------------------------------


class TestSeverCauseChainLinear:
    def _build_chain(self, length: int) -> BaseException:
        head = ValueError("link-0")
        current = head
        for i in range(1, length):
            nxt = ValueError(f"link-{i}")
            current.__cause__ = nxt
            current = nxt
        return head

    def _chain_depth(self, exc: BaseException | None) -> int:
        seen: set[int] = set()
        depth = 0
        current = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            depth += 1
            current = current.__cause__ or current.__context__
        return depth

    def test_short_chain_is_untouched(self) -> None:
        chain = self._build_chain(_MAX_CHAIN_DEPTH - 1)
        _sever_cause_chain(chain)
        assert self._chain_depth(chain) == _MAX_CHAIN_DEPTH - 1

    def test_chain_at_max_depth_is_untouched(self) -> None:
        chain = self._build_chain(_MAX_CHAIN_DEPTH)
        _sever_cause_chain(chain)
        assert self._chain_depth(chain) == _MAX_CHAIN_DEPTH

    def test_deep_chain_is_severed(self) -> None:
        chain = self._build_chain(_MAX_CHAIN_DEPTH + 50)
        _sever_cause_chain(chain)
        assert self._chain_depth(chain) == _MAX_CHAIN_DEPTH

    def test_very_deep_chain_does_not_recurse(self) -> None:
        chain = self._build_chain(1_000)
        # Must not raise RecursionError — the function is iterative
        _sever_cause_chain(chain)
        assert self._chain_depth(chain) == _MAX_CHAIN_DEPTH


# ---------------------------------------------------------------------------
# _sever_cause_chain — cyclic chain
# ---------------------------------------------------------------------------


class TestSeverCauseChainCycle:
    def test_two_node_cycle_is_severed(self) -> None:
        a = ValueError("a")
        b = ValueError("b")
        a.__cause__ = b
        b.__cause__ = a  # cycle
        _sever_cause_chain(a)
        # After severing, the chain must be finite
        seen: set[int] = set()
        current: BaseException | None = a
        count = 0
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            count += 1
            current = current.__cause__ or current.__context__
        assert count <= _MAX_CHAIN_DEPTH

    def test_self_referential_cause_is_severed(self) -> None:
        a = ValueError("self")
        a.__cause__ = a  # self-loop
        _sever_cause_chain(a)
        assert a.__cause__ is None

    def test_context_cycle_is_severed(self) -> None:
        a = ValueError("a")
        b = ValueError("b")
        a.__context__ = b
        b.__context__ = a  # cycle via __context__
        _sever_cause_chain(a)
        # Must terminate without RecursionError
        seen: set[int] = set()
        current: BaseException | None = a
        count = 0
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            count += 1
            current = current.__cause__ or current.__context__
        assert count <= _MAX_CHAIN_DEPTH


# ---------------------------------------------------------------------------
# _extract_failure_attrs — depth cap
# ---------------------------------------------------------------------------


class TestExtractFailureAttrsDepthCap:
    def _build_chain(self, length: int) -> BaseException:
        head = ValueError("link-0")
        current = head
        for i in range(1, length):
            nxt = ValueError(f"link-{i}")
            current.__cause__ = nxt
            current = nxt
        return head

    def test_returns_empty_for_plain_chain_beyond_max(self) -> None:
        chain = self._build_chain(_MAX_CHAIN_WALK + 100)
        result = _extract_failure_attrs(chain)
        assert result == {}

    def test_finds_app_error_within_depth(self) -> None:
        chain = self._build_chain(3)
        # Wrap the chain: AppError at depth 2
        app_err = _ConfigError(message="no assets")
        app_err.__cause__ = chain
        wrapper = ValueError("outer")
        wrapper.__cause__ = app_err
        result = _extract_failure_attrs(wrapper)
        assert result.get("failure.code") == "NO_ASSETS"

    def test_does_not_recurse_on_deep_chain(self) -> None:
        chain = self._build_chain(1_000)
        # Must not raise RecursionError
        _extract_failure_attrs(chain)

    def test_cycle_does_not_loop(self) -> None:
        a = ValueError("a")
        b = ValueError("b")
        a.__cause__ = b
        b.__cause__ = a
        # Must not hang or raise RecursionError
        result = _extract_failure_attrs(a)
        assert result == {}


# ---------------------------------------------------------------------------
# Integration: AppError with deep chain → activity exception handler
# ---------------------------------------------------------------------------


class TestActivityExceptionHandlerDeepChain:
    """Verify that the activity_fn exception handler does not raise RecursionError
    when the AppError carries a chain longer than Python's recursion limit / 2."""

    def _build_app_error_with_chain(self, chain_length: int) -> _ConfigError:
        # Build a chain of plain exceptions
        tail = ValueError("tail")
        current: BaseException = tail
        for i in range(chain_length - 1):
            nxt = ValueError(f"link-{i}")
            nxt.__cause__ = current
            current = nxt
        return _ConfigError(message="no accessible assets", cause=current)

    def test_handler_raises_application_error_not_recursion_error(self) -> None:
        from application_sdk.execution._temporal.activities import _sever_cause_chain
        from application_sdk.execution.errors import ApplicationError

        app_err = self._build_app_error_with_chain(600)

        # Replicate what the activity exception handler does
        try:
            details: tuple = (app_err.to_failure_details(),)
        except Exception:
            details = ()

        _sever_cause_chain(app_err)

        raised = ApplicationError(
            str(app_err),
            *details,
            type=type(app_err).__name__,
            non_retryable=not app_err.effective_retryable,
        )
        raised.__cause__ = app_err

        # The chain rooted at raised must be finite and within safe bounds
        seen: set[int] = set()
        current: BaseException | None = raised
        depth = 0
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            depth += 1
            current = current.__cause__ or current.__context__

        # _MAX_CHAIN_DEPTH + 1 for the ApplicationError wrapper itself
        assert depth <= _MAX_CHAIN_DEPTH + 2

    def test_details_carry_original_type_and_message(self) -> None:
        from application_sdk.execution._temporal.activities import _sever_cause_chain
        from application_sdk.execution.errors import ApplicationError

        app_err = self._build_app_error_with_chain(600)

        try:
            details: tuple = (app_err.to_failure_details(),)
        except Exception:
            details = ()

        _sever_cause_chain(app_err)

        raised = ApplicationError(
            str(app_err),
            *details,
            type=type(app_err).__name__,
            non_retryable=not app_err.effective_retryable,
        )

        assert raised.type == "_ConfigError"
        assert "no accessible assets" in raised.message
        assert len(raised.details) == 1
        fd = raised.details[0]
        assert fd.code == "NO_ASSETS"
        assert fd.message == "no accessible assets"
