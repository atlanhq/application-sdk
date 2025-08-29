"""Unit tests for lock management activities."""

from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import ConnectionError, RedisError, TimeoutError

from application_sdk.activities.lock_management import (
    acquire_distributed_lock,
    release_distributed_lock,
)
from application_sdk.clients.redis import LockReleaseResult
from application_sdk.common.error_codes import ActivityError


class TestAcquireDistributedLock:
    """Test cases for acquire_distributed_lock activity."""

    @pytest.fixture
    def mock_redis_client(self):
        """Create a mock Redis client."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        return mock_client

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_acquire_lock_success_first_try(
        self, mock_redis_class, mock_redis_client
    ):
        """Test successful lock acquisition on first attempt."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._acquire_lock.return_value = True

        # Act
        result = await acquire_distributed_lock(
            lock_name="test_resource",
            max_locks=5,
            ttl_seconds=100,
            owner_id="test_owner",
        )

        # Assert
        assert result["slot_id"] in range(0, 5)
        assert "test_resource" in result["resource_id"]
        assert result["owner_id"] == "test_owner"
        mock_redis_client._acquire_lock.assert_called_once()

    @patch("application_sdk.activities.lock_management.RedisClient")
    @patch("application_sdk.activities.lock_management.asyncio.sleep")
    async def test_acquire_lock_retry_then_success(
        self, mock_sleep, mock_redis_class, mock_redis_client
    ):
        """Test lock acquisition with retry logic."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        # First two attempts fail, third succeeds
        mock_redis_client._acquire_lock.side_effect = [False, False, True]
        mock_sleep.return_value = None

        # Act
        result = await acquire_distributed_lock(
            lock_name="test_resource",
            max_locks=3,
            ttl_seconds=60,
            owner_id="test_owner",
        )

        # Assert
        assert result["owner_id"] == "test_owner"
        assert mock_redis_client._acquire_lock.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_acquire_lock_redis_connection_error(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock acquisition with Redis connection error."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._acquire_lock.side_effect = ConnectionError(
            "Connection failed"
        )

        # Act & Assert
        with pytest.raises(ActivityError) as exc_info:
            await acquire_distributed_lock(
                lock_name="test_resource",
                max_locks=5,
                ttl_seconds=100,
                owner_id="test_owner",
            )

        assert "ATLAN-ACTIVITY-503-01" in str(exc_info.value)
        assert "Redis error during lock acquisition" in str(exc_info.value)

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_acquire_lock_redis_timeout_error(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock acquisition with Redis timeout error."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._acquire_lock.side_effect = TimeoutError(
            "Operation timed out"
        )

        # Act & Assert
        with pytest.raises(ActivityError) as exc_info:
            await acquire_distributed_lock(
                lock_name="test_resource",
                max_locks=5,
                ttl_seconds=100,
                owner_id="test_owner",
            )

        assert "ATLAN-ACTIVITY-503-01" in str(exc_info.value)
        assert "Redis error during lock acquisition" in str(exc_info.value)

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_acquire_lock_redis_auth_error(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock acquisition with Redis authentication error."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._acquire_lock.side_effect = RedisError(
            "Authentication failed"
        )

        # Act & Assert
        with pytest.raises(ActivityError) as exc_info:
            await acquire_distributed_lock(
                lock_name="test_resource",
                max_locks=5,
                ttl_seconds=100,
                owner_id="test_owner",
            )

        assert "ATLAN-ACTIVITY-503-01" in str(exc_info.value)
        assert "Redis error during lock acquisition" in str(exc_info.value)

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_acquire_lock_unexpected_error(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock acquisition with unexpected error."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._acquire_lock.side_effect = ValueError("Unexpected error")

        # Act & Assert
        with pytest.raises(ActivityError) as exc_info:
            await acquire_distributed_lock(
                lock_name="test_resource",
                max_locks=5,
                ttl_seconds=100,
                owner_id="test_owner",
            )

        assert "ATLAN-ACTIVITY-503-01" in str(exc_info.value)
        assert "Redis error during lock acquisition" in str(exc_info.value)

    @patch("application_sdk.activities.lock_management.APPLICATION_NAME", "test_app")
    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_acquire_lock_resource_id_format(
        self, mock_redis_class, mock_redis_client
    ):
        """Test that resource ID is formatted correctly."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._acquire_lock.return_value = True

        # Act
        result = await acquire_distributed_lock(
            lock_name="my_resource", max_locks=5, ttl_seconds=100, owner_id="test_owner"
        )

        # Assert
        expected_prefix = "test_app:my_resource:"
        assert result["resource_id"].startswith(expected_prefix)

        # Verify the call was made with correct resource_id format
        call_args = mock_redis_client._acquire_lock.call_args[0]
        assert call_args[0].startswith(expected_prefix)
        assert call_args[1] == "test_owner"
        assert call_args[2] == 100

    async def test_acquire_lock_invalid_max_locks_zero(self):
        """Test that max_locks=0 raises ActivityError."""
        with pytest.raises(ActivityError) as exc_info:
            await acquire_distributed_lock(
                lock_name="test_resource",
                max_locks=0,
                ttl_seconds=60,
                owner_id="test_owner",
            )

        assert "ATLAN-ACTIVITY-503-01" in str(exc_info.value)
        assert "max_locks must be greater than 0, got 0" in str(exc_info.value)

    async def test_acquire_lock_invalid_max_locks_negative(self):
        """Test that negative max_locks raises ActivityError."""
        with pytest.raises(ActivityError) as exc_info:
            await acquire_distributed_lock(
                lock_name="test_resource",
                max_locks=-5,
                ttl_seconds=60,
                owner_id="test_owner",
            )

        assert "ATLAN-ACTIVITY-503-01" in str(exc_info.value)
        assert "max_locks must be greater than 0, got -5" in str(exc_info.value)


class TestReleaseDistributedLock:
    """Test cases for release_distributed_lock activity."""

    @pytest.fixture
    def mock_redis_client(self):
        """Create a mock Redis client."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        return mock_client

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_release_lock_success(self, mock_redis_class, mock_redis_client):
        """Test successful lock release."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._release_lock.return_value = (True, LockReleaseResult.SUCCESS)

        # Act
        result = await release_distributed_lock(
            resource_id="test_app:test_resource:0", owner_id="test_owner"
        )

        # Assert
        assert result is True
        mock_redis_client._release_lock.assert_called_once_with(
            "test_app:test_resource:0", "test_owner"
        )

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_release_lock_already_released(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock release when lock was already released (TTL expired)."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._release_lock.return_value = (
            True,
            LockReleaseResult.ALREADY_RELEASED,
        )

        # Act
        result = await release_distributed_lock(
            resource_id="test_app:test_resource:0", owner_id="test_owner"
        )

        # Assert
        assert result is True  # Already released is considered success

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_release_lock_wrong_owner(self, mock_redis_class, mock_redis_client):
        """Test lock release with wrong owner."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._release_lock.return_value = (
            False,
            LockReleaseResult.WRONG_OWNER,
        )

        # Act
        result = await release_distributed_lock(
            resource_id="test_app:test_resource:0", owner_id="wrong_owner"
        )

        # Assert
        assert result is False  # Wrong owner should return False

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_release_lock_redis_connection_error(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock release with Redis connection error - should not raise."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._release_lock.side_effect = ConnectionError(
            "Connection failed"
        )

        # Act
        result = await release_distributed_lock(
            resource_id="test_app:test_resource:0", owner_id="test_owner"
        )

        # Assert
        assert result is False  # Infrastructure failure should return False, not raise

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_release_lock_redis_timeout_error(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock release with Redis timeout error - should not raise."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._release_lock.side_effect = TimeoutError(
            "Operation timed out"
        )

        # Act
        result = await release_distributed_lock(
            resource_id="test_app:test_resource:0", owner_id="test_owner"
        )

        # Assert
        assert result is False  # Infrastructure failure should return False, not raise

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_release_lock_redis_auth_error(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock release with Redis authentication error - should not raise."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._release_lock.side_effect = RedisError(
            "Authentication failed"
        )

        # Act
        result = await release_distributed_lock(
            resource_id="test_app:test_resource:0", owner_id="test_owner"
        )

        # Assert
        assert result is False  # Infrastructure failure should return False, not raise

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_release_lock_unexpected_error(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock release with unexpected error - should not raise."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._release_lock.side_effect = ValueError("Unexpected error")

        # Act
        result = await release_distributed_lock(
            resource_id="test_app:test_resource:0", owner_id="test_owner"
        )

        # Assert
        assert result is False  # Any error should return False, not raise

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_release_lock_default_owner_id(
        self, mock_redis_class, mock_redis_client
    ):
        """Test lock release with default owner ID."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._release_lock.return_value = (True, LockReleaseResult.SUCCESS)

        # Act
        result = await release_distributed_lock(
            resource_id="test_app:test_resource:0"
            # owner_id not provided, should use default
        )

        # Assert
        assert result is True
        mock_redis_client._release_lock.assert_called_once_with(
            "test_app:test_resource:0", "default_owner"
        )


class TestLockManagementIntegration:
    """Integration tests for lock management activities."""

    @pytest.fixture
    def mock_redis_client(self):
        """Create a mock Redis client."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        return mock_client

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_acquire_then_release_success_flow(
        self, mock_redis_class, mock_redis_client
    ):
        """Test complete acquire -> release flow."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._acquire_lock.return_value = True
        mock_redis_client._release_lock.return_value = (True, LockReleaseResult.SUCCESS)

        # Act - Acquire lock
        acquire_result = await acquire_distributed_lock(
            lock_name="integration_test",
            max_locks=3,
            ttl_seconds=60,
            owner_id="integration_owner",
        )

        # Act - Release lock
        release_result = await release_distributed_lock(
            resource_id=acquire_result["resource_id"],
            owner_id=acquire_result["owner_id"],
        )

        # Assert
        assert acquire_result["owner_id"] == "integration_owner"
        assert "integration_test" in acquire_result["resource_id"]
        assert release_result is True

        # Verify calls
        mock_redis_client._acquire_lock.assert_called_once()
        mock_redis_client._release_lock.assert_called_once_with(
            acquire_result["resource_id"], "integration_owner"
        )

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_acquire_success_release_wrong_owner(
        self, mock_redis_class, mock_redis_client
    ):
        """Test acquire success but release with wrong owner."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._acquire_lock.return_value = True
        mock_redis_client._release_lock.return_value = (
            False,
            LockReleaseResult.WRONG_OWNER,
        )

        # Act - Acquire lock
        acquire_result = await acquire_distributed_lock(
            lock_name="integration_test",
            max_locks=3,
            ttl_seconds=60,
            owner_id="correct_owner",
        )

        # Act - Try to release with wrong owner
        release_result = await release_distributed_lock(
            resource_id=acquire_result["resource_id"], owner_id="wrong_owner"
        )

        # Assert
        assert acquire_result["owner_id"] == "correct_owner"
        assert release_result is False  # Wrong owner should fail gracefully

    @patch("application_sdk.activities.lock_management.RedisClient")
    async def test_acquire_failure_no_release_needed(
        self, mock_redis_class, mock_redis_client
    ):
        """Test that failed acquisition doesn't require release."""
        # Arrange
        mock_redis_class.return_value = mock_redis_client
        mock_redis_client._acquire_lock.side_effect = ConnectionError("Redis down")

        # Act & Assert - Acquire should fail
        with pytest.raises(ActivityError):
            await acquire_distributed_lock(
                lock_name="integration_test",
                max_locks=3,
                ttl_seconds=60,
                owner_id="test_owner",
            )

        # Verify no release was attempted
        mock_redis_client._release_lock.assert_not_called()
