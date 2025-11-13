"""Unit tests for method-level lock decorator."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.decorators.method_lock import lock_per_run


class TestLockPerRun:
    """Test lock_per_run decorator."""

    @patch("application_sdk.decorators.method_lock.RedisClientAsync")
    @patch("application_sdk.decorators.method_lock.activity")
    @patch("application_sdk.decorators.method_lock.IS_LOCKING_DISABLED", False)
    async def test_lock_per_run_acquires_and_releases_success(
        self, mock_activity, mock_redis_client_class
    ):
        """Test successful lock acquisition and release."""
        # Setup mocks
        mock_activity.info.return_value = SimpleNamespace(workflow_run_id="run-123")
        mock_client = AsyncMock()
        mock_client._acquire_lock.return_value = True
        mock_client._release_lock.return_value = (True, None)
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute
        @lock_per_run()
        async def sample(x: int) -> str:
            return f"ok-{x}"

        result = await sample(42)

        # Verify
        assert result == "ok-42"
        mock_client._acquire_lock.assert_called_once()
        mock_client._release_lock.assert_called_once()
        # Verify lock key format
        call_args = mock_client._acquire_lock.call_args
        assert call_args[0][0].endswith(":run:run-123")
        assert ":meth:sample:" in call_args[0][0]

    @patch("application_sdk.decorators.method_lock.RedisClientAsync")
    @patch("application_sdk.decorators.method_lock.activity")
    @patch("application_sdk.decorators.method_lock.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.decorators.method_lock.asyncio.sleep")
    async def test_lock_per_run_retries_on_failure(
        self, mock_sleep, mock_activity, mock_redis_client_class
    ):
        """Test lock retries when acquisition fails initially."""
        # Setup mocks
        mock_activity.info.return_value = SimpleNamespace(workflow_run_id="run-456")
        mock_client = AsyncMock()
        # First call fails, second succeeds
        mock_client._acquire_lock.side_effect = [False, True]
        mock_client._release_lock.return_value = (True, None)
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute
        @lock_per_run()
        async def sample() -> str:
            return "ok"

        result = await sample()

        # Verify
        assert result == "ok"
        assert mock_client._acquire_lock.call_count == 2
        mock_sleep.assert_called_once()  # Should sleep once between retries
        mock_client._release_lock.assert_called_once()

    @patch("application_sdk.decorators.method_lock.activity")
    @patch("application_sdk.decorators.method_lock.IS_LOCKING_DISABLED", True)
    async def test_lock_per_run_skips_when_disabled(self, mock_activity):
        """Test lock is skipped when IS_LOCKING_DISABLED is True."""
        # Setup mock
        mock_activity.info.return_value = SimpleNamespace(workflow_run_id="run-abc")

        # Execute
        @lock_per_run()
        async def sample() -> str:
            return "ok"

        result = await sample()

        # Verify - function executes without Redis calls
        assert result == "ok"

    @patch("application_sdk.decorators.method_lock.RedisClientAsync")
    @patch("application_sdk.decorators.method_lock.activity")
    @patch("application_sdk.decorators.method_lock.IS_LOCKING_DISABLED", False)
    async def test_lock_per_run_releases_on_exception(
        self, mock_activity, mock_redis_client_class
    ):
        """Test lock is released even when wrapped function raises exception."""
        # Setup mocks
        mock_activity.info.return_value = SimpleNamespace(workflow_run_id="run-err")
        mock_client = AsyncMock()
        mock_client._acquire_lock.return_value = True
        mock_client._release_lock.return_value = (True, None)
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute
        @lock_per_run()
        async def failing() -> None:
            raise RuntimeError("boom")

        # Verify exception is raised
        with pytest.raises(RuntimeError, match="boom"):
            await failing()

        # Verify release was called even on exception
        mock_client._acquire_lock.assert_called_once()
        mock_client._release_lock.assert_called_once()

    @patch("application_sdk.decorators.method_lock.RedisClientAsync")
    @patch("application_sdk.decorators.method_lock.activity")
    @patch("application_sdk.decorators.method_lock.IS_LOCKING_DISABLED", False)
    async def test_lock_per_run_custom_lock_name(
        self, mock_activity, mock_redis_client_class
    ):
        """Test custom lock name is used when provided."""
        # Setup mocks
        mock_activity.info.return_value = SimpleNamespace(workflow_run_id="run-custom")
        mock_client = AsyncMock()
        mock_client._acquire_lock.return_value = True
        mock_client._release_lock.return_value = (True, None)
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute
        @lock_per_run(lock_name="custom_lock")
        async def sample() -> str:
            return "ok"

        await sample()

        # Verify custom lock name in resource_id
        call_args = mock_client._acquire_lock.call_args
        resource_id = call_args[0][0]
        assert ":meth:custom_lock:" in resource_id
        assert ":meth:sample:" not in resource_id

    @patch("application_sdk.decorators.method_lock.RedisClientAsync")
    @patch("application_sdk.decorators.method_lock.activity")
    @patch("application_sdk.decorators.method_lock.IS_LOCKING_DISABLED", False)
    async def test_lock_per_run_custom_ttl(
        self, mock_activity, mock_redis_client_class
    ):
        """Test custom TTL is used when provided."""
        # Setup mocks
        mock_activity.info.return_value = SimpleNamespace(workflow_run_id="run-ttl")
        mock_client = AsyncMock()
        mock_client._acquire_lock.return_value = True
        mock_client._release_lock.return_value = (True, None)
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute
        @lock_per_run(ttl_seconds=300)
        async def sample() -> str:
            return "ok"

        await sample()

        # Verify custom TTL is passed to acquire_lock
        call_args = mock_client._acquire_lock.call_args
        ttl_seconds = call_args[0][2]
        assert ttl_seconds == 300
