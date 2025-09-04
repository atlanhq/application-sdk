"""Unit tests for lock management activities."""

from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.activities.lock_management import (
    acquire_distributed_lock,
    release_distributed_lock,
)
from application_sdk.clients.redis import LockReleaseResult
from application_sdk.common.error_codes import ActivityError


class TestAcquireDistributedLock:
    """Test acquire_distributed_lock activity."""

    @patch("application_sdk.activities.lock_management.RedisClientAsync")
    async def test_acquire_lock_success_first_try(self, mock_redis_client_class):
        """Test successful lock acquisition on first try."""
        # Setup mock
        mock_client = AsyncMock()
        mock_client._acquire_lock.return_value = True
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute
        result = await acquire_distributed_lock("test_resource", 5, 100, "owner1")

        # Verify
        assert "slot_id" in result
        assert "resource_id" in result
        assert "owner_id" in result
        assert result["owner_id"] == "owner1"
        assert 0 <= result["slot_id"] < 5

    @patch("application_sdk.activities.lock_management.RedisClientAsync")
    async def test_acquire_lock_invalid_max_locks(self, mock_redis_client_class):
        """Test lock acquisition with invalid max_locks."""
        with pytest.raises(ActivityError) as exc_info:
            await acquire_distributed_lock("test_resource", 0, 100, "owner1")

        assert "ATLAN-ACTIVITY-503-01" in str(exc_info.value)
        assert "max_locks must be greater than 0" in str(exc_info.value)

    @patch("application_sdk.activities.lock_management.RedisClientAsync")
    async def test_acquire_lock_redis_error(self, mock_redis_client_class):
        """Test lock acquisition with Redis error."""
        # Setup mock to raise exception
        mock_client = AsyncMock()
        mock_client._acquire_lock.side_effect = Exception("Redis connection failed")
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute and verify
        with pytest.raises(ActivityError) as exc_info:
            await acquire_distributed_lock("test_resource", 5, 100, "owner1")

        assert "ATLAN-ACTIVITY-503-01" in str(exc_info.value)
        assert "Redis error during lock acquisition" in str(exc_info.value)


class TestReleaseDistributedLock:
    """Test release_distributed_lock activity."""

    @patch("application_sdk.activities.lock_management.RedisClientAsync")
    async def test_release_lock_success(self, mock_redis_client_class):
        """Test successful lock release."""
        # Setup mock
        mock_client = AsyncMock()
        mock_client._release_lock.return_value = (True, LockReleaseResult.SUCCESS)
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute
        result = await release_distributed_lock("test:resource:0", "owner1")

        # Verify
        assert result is True
        mock_client._release_lock.assert_called_once_with("test:resource:0", "owner1")

    @patch("application_sdk.activities.lock_management.RedisClientAsync")
    async def test_release_lock_wrong_owner(self, mock_redis_client_class):
        """Test lock release with wrong owner."""
        # Setup mock
        mock_client = AsyncMock()
        mock_client._release_lock.return_value = (False, LockReleaseResult.WRONG_OWNER)
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute
        result = await release_distributed_lock("test:resource:0", "wrong_owner")

        # Verify
        assert result is False

    @patch("application_sdk.activities.lock_management.RedisClientAsync")
    async def test_release_lock_redis_error(self, mock_redis_client_class):
        """Test lock release with Redis error (should not raise)."""
        # Setup mock to raise exception
        mock_client = AsyncMock()
        mock_client._release_lock.side_effect = Exception("Redis connection failed")
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Execute - should not raise exception
        result = await release_distributed_lock("test:resource:0", "owner1")

        # Verify - returns False on error (best-effort cleanup)
        assert result is False


class TestLockManagementIntegration:
    """Integration tests for lock management activities."""

    @patch("application_sdk.activities.lock_management.RedisClientAsync")
    async def test_complete_lock_cycle(self, mock_redis_client_class):
        """Test complete acquire -> release cycle."""
        # Setup mock
        mock_client = AsyncMock()
        mock_client._acquire_lock.return_value = True
        mock_client._release_lock.return_value = (True, LockReleaseResult.SUCCESS)
        mock_redis_client_class.return_value.__aenter__.return_value = mock_client

        # Acquire lock
        acquire_result = await acquire_distributed_lock(
            "test_resource", 5, 100, "owner1"
        )

        # Release lock
        release_result = await release_distributed_lock(
            acquire_result["resource_id"], acquire_result["owner_id"]
        )

        # Verify
        assert acquire_result["owner_id"] == "owner1"
        assert release_result is True
