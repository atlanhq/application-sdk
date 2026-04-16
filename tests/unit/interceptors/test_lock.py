"""Unit tests for the distributed lock interceptor.

Regression test for BLDX-1025: lock must be held until the business activity
completes, not released when start_activity returns a handle.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

from application_sdk.execution._temporal.interceptors.lock import (
    RedisLockOutboundInterceptor,
)


@dataclass
class MockStartActivityInput:
    """Mock StartActivityInput for testing."""

    activity: str = "test_activity"
    args: Sequence[Any] = field(default_factory=list)
    schedule_to_close_timeout: timedelta | None = timedelta(minutes=5)


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

        # Mock the next interceptor's start_activity to return a handle
        # that tracks when it is awaited (simulating activity completion)
        class MockActivityHandle:
            def __await__(self_handle):
                async def _resolve():
                    call_order.append("activity_completed")
                    return {"status": "done"}

                return _resolve().__await__()

        mock_next = AsyncMock()
        mock_next.start_activity = AsyncMock(return_value=MockActivityHandle())

        # Mock workflow module
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

            # execute_activity: first call = acquire lock, second call = release lock
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

            # Create interceptor with a mock activity that has lock metadata
            mock_activity_fn = MagicMock()
            mock_activity_fn.__lock_metadata__ = {
                "lock_name": "test-lock",
                "max_locks": 5,
            }
            activities = {"test_activity": mock_activity_fn}

            interceptor = RedisLockOutboundInterceptor(mock_next, activities)

            input_data = MockStartActivityInput()
            await interceptor.start_activity(input_data)

        # CRITICAL ASSERTION: lock must be released AFTER activity completes
        assert "lock_acquired" in call_order
        assert "activity_completed" in call_order
        assert "lock_released" in call_order
        assert call_order.index("lock_acquired") < call_order.index(
            "activity_completed"
        ), "Lock must be acquired before activity starts"
        assert call_order.index("activity_completed") < call_order.index(
            "lock_released"
        ), "Activity must complete before lock is released"
