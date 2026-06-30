"""Regression tests for BLDX-1512.

Temporal's failure serializer recurses into __cause__/__context__ chains without
a depth bound.  A deep or cyclic chain overflows the call stack and replaces the
real error with "Failed building exception result: maximum recursion depth
exceeded".

These tests verify that:
  1. _sever_cause_chain caps the chain at _MAX_CHAIN_DEPTH — linear and cyclic.
  2. _extract_failure_attrs stops at _MAX_CHAIN_WALK even on a deep chain.
  3. An AppError with a 600-link chain does not raise RecursionError when run
     through the real activity_fn produced by create_activity_from_task().
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest

from application_sdk.contracts.base import Input, Output
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
# _sever_cause_chain — linear __cause__ chains and __suppress_context__
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

    def test_suppress_context_stops_descent(self) -> None:
        # Simulates `raise a from None`: __suppress_context__ is True so the
        # walker must not follow __context__ even if it carries a long chain.
        a = ValueError("a")
        b = ValueError("b")
        c = ValueError("c")
        b.__cause__ = c  # b has its own cause — untouched if walker skips b
        a.__context__ = b
        a.__suppress_context__ = True
        _sever_cause_chain(a)
        # The walker exited at `a` without entering `b`; b's chain is intact.
        assert b.__cause__ is c

    def test_context_only_chain_depth_cap(self) -> None:
        # Build a deep chain using only __context__ (no __cause__).
        head = ValueError("head")
        current = head
        for i in range(_MAX_CHAIN_DEPTH + 20):
            nxt = ValueError(f"ctx-{i}")
            current.__context__ = nxt
            current = nxt
        _sever_cause_chain(head)
        assert self._chain_depth(head) <= _MAX_CHAIN_DEPTH


# ---------------------------------------------------------------------------
# _sever_cause_chain — cyclic chains
# ---------------------------------------------------------------------------


class TestSeverCauseChainCycle:
    def test_two_node_cycle_is_severed(self) -> None:
        a = ValueError("a")
        b = ValueError("b")
        a.__cause__ = b
        b.__cause__ = a  # cycle
        _sever_cause_chain(a)
        # The sever point is b (it would next point back to a, which is in seen).
        # Assert the mutation directly rather than re-walking with id() guards
        # (which would also terminate on the cycle regardless of the fix).
        assert b.__cause__ is None
        assert b.__context__ is None

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
        # b would next point back to a (in seen) — assert the link is cut.
        assert b.__context__ is None


# ---------------------------------------------------------------------------
# _extract_failure_attrs — depth cap
# ---------------------------------------------------------------------------


class TestExtractFailureAttrsDepthCap:
    def _build_plain_chain(self, length: int) -> BaseException:
        head = ValueError("link-0")
        current = head
        for i in range(1, length):
            nxt = ValueError(f"link-{i}")
            current.__cause__ = nxt
            current = nxt
        return head

    def test_app_error_beyond_cap_is_not_found(self) -> None:
        # _MAX_CHAIN_WALK plain links at positions 1…_MAX_CHAIN_WALK, then
        # AppError at position _MAX_CHAIN_WALK+1 — just outside the walk window.
        head = ValueError("link-0")
        current = head
        for i in range(1, _MAX_CHAIN_WALK):
            nxt = ValueError(f"link-{i}")
            current.__cause__ = nxt
            current = nxt
        current.__cause__ = _ConfigError(message="hidden")
        result = _extract_failure_attrs(head)
        assert result == {}

    def test_app_error_at_cap_boundary_is_found(self) -> None:
        # _MAX_CHAIN_WALK-1 plain links, then AppError at position _MAX_CHAIN_WALK
        # — the last item the walker inspects.
        head = ValueError("link-0")
        current = head
        for i in range(1, _MAX_CHAIN_WALK - 1):
            nxt = ValueError(f"link-{i}")
            current.__cause__ = nxt
            current = nxt
        current.__cause__ = _ConfigError(message="visible")
        result = _extract_failure_attrs(head)
        assert result.get("failure.code") == "NO_ASSETS"

    def test_finds_app_error_within_depth(self) -> None:
        chain = self._build_plain_chain(3)
        # AppError at depth 2
        app_err = _ConfigError(message="no assets")
        app_err.__cause__ = chain
        wrapper = ValueError("outer")
        wrapper.__cause__ = app_err
        result = _extract_failure_attrs(wrapper)
        assert result.get("failure.code") == "NO_ASSETS"

    def test_does_not_recurse_on_deep_chain(self) -> None:
        chain = self._build_plain_chain(1_000)
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
# Integration: AppError with deep chain → real activity_fn
# ---------------------------------------------------------------------------


class _DeepErrIn(Input, allow_unbounded_fields=True):
    x: str = ""


class _DeepErrOut(Output, allow_unbounded_fields=True):
    y: str = ""


class _DeepMsgIn(Input, allow_unbounded_fields=True):
    x: str = ""


class _DeepMsgOut(Output, allow_unbounded_fields=True):
    y: str = ""


class TestActivityExceptionHandlerDeepChain:
    """Verify the activity_fn exception handler does not raise RecursionError
    when the AppError carries a chain longer than Python's recursion limit / 2.

    Uses create_activity_from_task() rather than reconstructing the handler
    inline so regressions in the actual handler path are caught.
    """

    def setup_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def _build_deep_chain_error(self, chain_length: int) -> _ConfigError:
        tail = ValueError("tail")
        current: BaseException = tail
        for i in range(chain_length - 1):
            nxt = ValueError(f"link-{i}")
            nxt.__cause__ = current
            current = nxt
        return _ConfigError(message="no accessible assets", cause=current)

    @pytest.mark.asyncio
    async def test_handler_raises_application_error_not_recursion_error(
        self,
    ) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.registry import TaskRegistry
        from application_sdk.app.task import task
        from application_sdk.execution._temporal import activities as activities_module
        from application_sdk.execution._temporal.activities import (
            TaskContext,
            create_activity_from_task,
        )
        from application_sdk.execution.errors import ApplicationError

        deep_err = self._build_deep_chain_error(600)

        class _DeepErrApp(App):
            @task(timeout_seconds=60)
            async def deep_fail(self, input: _DeepErrIn) -> _DeepErrOut:
                raise deep_err

            async def run(self, input: _DeepErrIn) -> _DeepErrOut:  # type: ignore[override]
                return await self.deep_fail(input)

        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_deep-err-app")
        deep_fail_task = next(t for t in tasks if t.name == "deep_fail")
        activity_fn = create_activity_from_task(deep_fail_task)

        ctx = TaskContext(
            app_name="_deep-err-app",
            task_name="deep_fail",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-deep"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(ctx, _DeepErrIn(x=""))

        raised = exc_info.value
        # The chain rooted at raised must be finite and within safe bounds
        seen: set[int] = set()
        current: BaseException | None = raised
        depth = 0
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            depth += 1
            current = current.__cause__ or current.__context__

        # +1 for the ApplicationError wrapper itself, +1 buffer for __context__
        assert depth <= _MAX_CHAIN_DEPTH + 2
        assert raised.type == "_ConfigError"

    @pytest.mark.asyncio
    async def test_details_carry_original_type_and_message(self) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.registry import TaskRegistry
        from application_sdk.app.task import task
        from application_sdk.execution._temporal import activities as activities_module
        from application_sdk.execution._temporal.activities import (
            TaskContext,
            create_activity_from_task,
        )
        from application_sdk.execution.errors import ApplicationError

        deep_err = self._build_deep_chain_error(600)

        class _DeepMsgApp(App):
            @task(timeout_seconds=60)
            async def deep_fail(self, input: _DeepMsgIn) -> _DeepMsgOut:
                raise deep_err

            async def run(self, input: _DeepMsgIn) -> _DeepMsgOut:  # type: ignore[override]
                return await self.deep_fail(input)

        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_deep-msg-app")
        deep_fail_task = next(t for t in tasks if t.name == "deep_fail")
        activity_fn = create_activity_from_task(deep_fail_task)

        ctx = TaskContext(
            app_name="_deep-msg-app",
            task_name="deep_fail",
            run_id="run-2",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-msg"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(ctx, _DeepMsgIn(x=""))

        raised = exc_info.value
        assert raised.type == "_ConfigError"
        assert "no accessible assets" in raised.message
        assert len(raised.details) == 1
        fd = raised.details[0]
        assert fd.code == "NO_ASSETS"
        assert fd.message == "no accessible assets"
