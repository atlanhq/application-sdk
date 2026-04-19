"""Tests for RedisLockInterceptor and RedisLockOutboundInterceptor."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.constants import LOCK_METADATA_KEY


class _AwaitableHandle:
    """Mock activity handle that is awaitable (resolves to a result)."""

    def __init__(self, result=None):
        self._result = result

    def __await__(self):
        async def _resolve():
            return self._result

        return _resolve().__await__()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_activity_fn(lock_name: str = "my_lock", max_locks: int = 5):
    """Create a mock activity function with lock metadata."""
    fn = MagicMock()
    setattr(fn, LOCK_METADATA_KEY, {"lock_name": lock_name, "max_locks": max_locks})
    return fn


def _make_start_activity_input(
    activity_name: str = "my_activity",
    schedule_to_close_timeout: timedelta | None = timedelta(minutes=10),
):
    """Create a mock StartActivityInput."""
    inp = MagicMock()
    inp.activity = activity_name
    inp.schedule_to_close_timeout = schedule_to_close_timeout
    return inp


def _make_outbound_interceptor(activities: dict | None = None):
    """Build a RedisLockOutboundInterceptor with a mock next interceptor."""
    from application_sdk.execution._temporal.interceptors.lock import (
        RedisLockOutboundInterceptor,
    )

    if activities is None:
        activities = {}
    next_interceptor = MagicMock()
    next_interceptor.start_activity = AsyncMock(return_value=_AwaitableHandle())
    return RedisLockOutboundInterceptor(next_interceptor, activities), next_interceptor


# ---------------------------------------------------------------------------
# RedisLockInterceptor (top-level)
# ---------------------------------------------------------------------------


class TestRedisLockInterceptor:
    """Tests for the top-level RedisLockInterceptor class."""

    def test_workflow_interceptor_class_returns_inbound_type(self):
        """workflow_interceptor_class() returns a WorkflowInboundInterceptor subclass."""
        from temporalio.worker import WorkflowInboundInterceptor

        from application_sdk.execution._temporal.interceptors.lock import (
            RedisLockInterceptor,
        )

        interceptor = RedisLockInterceptor(activities={"act": MagicMock()})
        input_mock = MagicMock()
        cls = interceptor.workflow_interceptor_class(input_mock)
        assert cls is not None
        assert issubclass(cls, WorkflowInboundInterceptor)


# ---------------------------------------------------------------------------
# RedisLockOutboundInterceptor.start_activity — skip path
# ---------------------------------------------------------------------------


class TestStartActivitySkipLock:
    """Cases where the lock is NOT acquired."""

    async def test_no_lock_metadata_passes_through(self):
        """Activity without lock metadata delegates directly to next."""
        plain_fn = MagicMock(spec=[])  # no LOCK_METADATA_KEY attribute
        interceptor, next_mock = _make_outbound_interceptor(
            activities={"plain_act": plain_fn}
        )
        inp = _make_start_activity_input(activity_name="plain_act")

        result = await interceptor.start_activity(inp)

        next_mock.start_activity.assert_awaited_once_with(inp)
        assert result is next_mock.start_activity.return_value

    async def test_unknown_activity_passes_through(self):
        """Activity not in the activities dict delegates directly to next."""
        interceptor, next_mock = _make_outbound_interceptor(activities={})
        inp = _make_start_activity_input(activity_name="unknown")

        await interceptor.start_activity(inp)

        next_mock.start_activity.assert_awaited_once_with(inp)

    async def test_locking_disabled_passes_through(self):
        """When IS_LOCKING_DISABLED is True, activity passes through."""
        lock_fn = _make_activity_fn()
        interceptor, next_mock = _make_outbound_interceptor(
            activities={"my_activity": lock_fn}
        )
        inp = _make_start_activity_input()

        with patch(
            "application_sdk.execution._temporal.interceptors.lock.IS_LOCKING_DISABLED",
            True,
        ):
            await interceptor.start_activity(inp)

        next_mock.start_activity.assert_awaited_once_with(inp)


# ---------------------------------------------------------------------------
# RedisLockOutboundInterceptor.start_activity — lock path
# ---------------------------------------------------------------------------


class TestStartActivityWithLock:
    """Cases where lock acquisition is expected."""

    async def test_missing_schedule_to_close_raises(self):
        """Activity with @needs_lock but no schedule_to_close_timeout raises."""
        from application_sdk.common.error_codes import WorkflowError

        lock_fn = _make_activity_fn()
        interceptor, _ = _make_outbound_interceptor(activities={"my_activity": lock_fn})
        inp = _make_start_activity_input(schedule_to_close_timeout=None)

        with patch(
            "application_sdk.execution._temporal.interceptors.lock.IS_LOCKING_DISABLED",
            False,
        ):
            with pytest.raises(WorkflowError):
                await interceptor.start_activity(inp)

    async def test_acquire_execute_release_sequence(self):
        """Lock is acquired, business activity runs, then lock is released."""
        lock_fn = _make_activity_fn(lock_name="test_lock", max_locks=3)
        interceptor, next_mock = _make_outbound_interceptor(
            activities={"my_activity": lock_fn}
        )
        inp = _make_start_activity_input()

        lock_result = {"resource_id": "lock-123", "owner_id": "app:run-1"}

        # Track call order
        call_order = []

        async def mock_execute_activity(activity_name, *args, **kwargs):
            call_order.append(activity_name)
            if activity_name == "acquire_distributed_lock":
                return lock_result
            return None

        async def mock_start_activity(input_arg):
            call_order.append("business_activity")
            return _AwaitableHandle(result={"status": "done"})

        next_mock.start_activity = AsyncMock(side_effect=mock_start_activity)

        wf_info = MagicMock()
        wf_info.run_id = "run-1"
        wf_info.execution_timeout = timedelta(hours=1)

        with (
            patch(
                "application_sdk.execution._temporal.interceptors.lock.IS_LOCKING_DISABLED",
                False,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.execute_activity",
                side_effect=mock_execute_activity,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.info",
                return_value=wf_info,
            ),
        ):
            await interceptor.start_activity(inp)

        assert call_order == [
            "acquire_distributed_lock",
            "business_activity",
            "release_distributed_lock",
        ]

    async def test_lock_released_even_when_activity_fails(self):
        """Lock release runs in finally block even when business activity raises."""
        lock_fn = _make_activity_fn()
        interceptor, next_mock = _make_outbound_interceptor(
            activities={"my_activity": lock_fn}
        )
        inp = _make_start_activity_input()

        lock_result = {"resource_id": "lock-456", "owner_id": "app:run-2"}
        released = []

        async def mock_execute_activity(activity_name, *args, **kwargs):
            if activity_name == "acquire_distributed_lock":
                return lock_result
            if activity_name == "release_distributed_lock":
                released.append(True)
            return None

        next_mock.start_activity = AsyncMock(
            side_effect=RuntimeError("activity failed")
        )

        wf_info = MagicMock()
        wf_info.run_id = "run-2"
        wf_info.execution_timeout = timedelta(hours=1)

        with (
            patch(
                "application_sdk.execution._temporal.interceptors.lock.IS_LOCKING_DISABLED",
                False,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.execute_activity",
                side_effect=mock_execute_activity,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.info",
                return_value=wf_info,
            ),
        ):
            with pytest.raises(RuntimeError, match="activity failed"):
                await interceptor.start_activity(inp)

        assert released, "Lock should be released even on activity failure"

    async def test_lock_release_failure_is_silent(self):
        """A failed lock release is swallowed (TTL handles cleanup)."""
        lock_fn = _make_activity_fn()
        interceptor, next_mock = _make_outbound_interceptor(
            activities={"my_activity": lock_fn}
        )
        inp = _make_start_activity_input()

        lock_result = {"resource_id": "lock-789", "owner_id": "app:run-3"}
        call_count = {"acquire": 0, "release": 0}

        async def mock_execute_activity(activity_name, *args, **kwargs):
            if activity_name == "acquire_distributed_lock":
                call_count["acquire"] += 1
                return lock_result
            if activity_name == "release_distributed_lock":
                call_count["release"] += 1
                raise ConnectionError("redis down")
            return None

        next_mock.start_activity = AsyncMock(return_value=_AwaitableHandle())

        wf_info = MagicMock()
        wf_info.run_id = "run-3"
        wf_info.execution_timeout = timedelta(hours=1)

        with (
            patch(
                "application_sdk.execution._temporal.interceptors.lock.IS_LOCKING_DISABLED",
                False,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.execute_activity",
                side_effect=mock_execute_activity,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.info",
                return_value=wf_info,
            ),
        ):
            # Should NOT raise despite release failure
            await interceptor.start_activity(inp)

        assert call_count["release"] == 1

    async def test_lock_not_released_if_acquire_fails(self):
        """When lock acquisition fails, release is NOT attempted."""
        lock_fn = _make_activity_fn()
        interceptor, next_mock = _make_outbound_interceptor(
            activities={"my_activity": lock_fn}
        )
        inp = _make_start_activity_input()

        released = []

        async def mock_execute_activity(activity_name, *args, **kwargs):
            if activity_name == "acquire_distributed_lock":
                raise TimeoutError("could not acquire lock")
            if activity_name == "release_distributed_lock":
                released.append(True)
            return None

        wf_info = MagicMock()
        wf_info.run_id = "run-4"
        wf_info.execution_timeout = timedelta(hours=1)

        with (
            patch(
                "application_sdk.execution._temporal.interceptors.lock.IS_LOCKING_DISABLED",
                False,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.execute_activity",
                side_effect=mock_execute_activity,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.info",
                return_value=wf_info,
            ),
        ):
            with pytest.raises(TimeoutError, match="could not acquire lock"):
                await interceptor.start_activity(inp)

        assert not released, "Release should NOT be called when acquire fails"

    async def test_ttl_computed_from_schedule_to_close(self):
        """ttl_seconds is derived from schedule_to_close_timeout."""
        lock_fn = _make_activity_fn()
        interceptor, next_mock = _make_outbound_interceptor(
            activities={"my_activity": lock_fn}
        )
        inp = _make_start_activity_input(schedule_to_close_timeout=timedelta(minutes=5))

        captured_args = {}

        async def mock_execute_activity(activity_name, *args, **kwargs):
            if activity_name == "acquire_distributed_lock":
                captured_args["args"] = kwargs.get("args", args)
                return {"resource_id": "lock-x", "owner_id": "app:run-x"}
            return None

        next_mock.start_activity = AsyncMock(return_value=_AwaitableHandle())

        wf_info = MagicMock()
        wf_info.run_id = "run-x"
        wf_info.execution_timeout = timedelta(hours=1)

        with (
            patch(
                "application_sdk.execution._temporal.interceptors.lock.IS_LOCKING_DISABLED",
                False,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.execute_activity",
                side_effect=mock_execute_activity,
            ),
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow.info",
                return_value=wf_info,
            ),
        ):
            await interceptor.start_activity(inp)

        # ttl_seconds = 5 minutes = 300 seconds, should be 3rd positional arg
        acquire_args = captured_args["args"]
        assert acquire_args[2] == 300  # ttl_seconds


# ---------------------------------------------------------------------------
# Regression: lock held until activity completes (BLDX-1025)
# ---------------------------------------------------------------------------


class TestLockHeldUntilActivityCompletes:
    """Regression: lock must not be released before business activity finishes."""

    @patch(
        "application_sdk.execution._temporal.interceptors.lock.IS_LOCKING_DISABLED",
        False,
    )
    @patch(
        "application_sdk.execution._temporal.interceptors.lock.APPLICATION_NAME",
        "test-app",
    )
    async def test_lock_released_after_activity_completion(self):
        """The lock release must happen AFTER the activity result is awaited."""
        call_order: list[str] = []

        class MockActivityHandle:
            def __await__(self_handle):
                async def _resolve():
                    call_order.append("activity_completed")
                    return {"status": "done"}

                return _resolve().__await__()

        mock_next = AsyncMock()
        mock_next.start_activity = AsyncMock(return_value=MockActivityHandle())

        mock_workflow_info = MagicMock()
        mock_workflow_info.run_id = "test-run-id"
        mock_workflow_info.execution_timeout = timedelta(minutes=10)

        lock_result = {"resource_id": "lock:test:0", "owner_id": "test-app:test-run-id"}

        with (
            patch(
                "application_sdk.execution._temporal.interceptors.lock.workflow"
            ) as mock_workflow,
        ):
            mock_workflow.info.return_value = mock_workflow_info

            async def mock_execute_activity(*args, **kwargs):
                activity_name = args[0] if args else kwargs.get("activity", "")
                if activity_name == "acquire_distributed_lock":
                    call_order.append("lock_acquired")
                    return lock_result
                elif activity_name == "release_distributed_lock":
                    call_order.append("lock_released")
                    return None

            mock_workflow.execute_activity = AsyncMock(
                side_effect=mock_execute_activity
            )
            mock_workflow.ActivityHandle = MagicMock

            mock_activity_fn = MagicMock()
            mock_activity_fn.__lock_metadata__ = {
                "lock_name": "test-lock",
                "max_locks": 5,
            }
            activities = {"test_activity": mock_activity_fn}

            from application_sdk.execution._temporal.interceptors.lock import (
                RedisLockOutboundInterceptor,
            )

            interceptor = RedisLockOutboundInterceptor(mock_next, activities)

            inp = MagicMock()
            inp.activity = "test_activity"
            inp.schedule_to_close_timeout = timedelta(minutes=5)
            await interceptor.start_activity(inp)

        assert "lock_acquired" in call_order
        assert "activity_completed" in call_order
        assert "lock_released" in call_order
        assert call_order.index("lock_acquired") < call_order.index(
            "activity_completed"
        ), "Lock must be acquired before activity starts"
        assert call_order.index("activity_completed") < call_order.index(
            "lock_released"
        ), "Activity must complete before lock is released"
