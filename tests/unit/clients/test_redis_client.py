"""Unit tests for Redis client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import ConnectionError, RedisError, TimeoutError

from application_sdk.clients.redis import LockReleaseResult, RedisClient
from application_sdk.common.error_codes import ClientError


class TestRedisClientInitialization:
    """Test cases for RedisClient initialization."""

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", True)
    def test_initialization_locking_disabled(self):
        """Test RedisClient initialization when locking is disabled."""
        client = RedisClient()
        assert client.redis_client is None

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "test_password")
    @patch("application_sdk.clients.redis.REDIS_HOST", "localhost")
    @patch("application_sdk.clients.redis.REDIS_PORT", "6379")
    def test_initialization_standalone_config(self):
        """Test RedisClient initialization with standalone configuration."""
        client = RedisClient()
        assert client.redis_client is None  # Not connected until _connect() is called

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "test_password")
    @patch(
        "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", "host1:26379,host2:26379"
    )
    def test_initialization_sentinel_config(self):
        """Test RedisClient initialization with Sentinel configuration."""
        client = RedisClient()
        assert client.redis_client is None  # Not connected until _connect() is called

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", None)
    def test_initialization_missing_password(self):
        """Test RedisClient initialization with missing password."""
        with pytest.raises(ClientError) as exc_info:
            RedisClient()
        assert "ATLAN-CLIENT-403-00" in str(exc_info.value)
        assert "REDIS_PASSWORD is required" in str(exc_info.value)

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "test_password")
    @patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", None)
    @patch("application_sdk.clients.redis.REDIS_HOST", None)
    def test_initialization_missing_host_config(self):
        """Test RedisClient initialization with missing host configuration."""
        with pytest.raises(ClientError) as exc_info:
            RedisClient()
        assert "ATLAN-CLIENT-403-00" in str(exc_info.value)
        assert (
            "REDIS_SENTINEL_HOSTS or REDIS_HOST/REDIS_PORT must be configured"
            in str(exc_info.value)
        )


class TestRedisClientConnection:
    """Test cases for Redis client connection management."""

    @pytest.fixture
    def redis_client(self):
        """Create a RedisClient instance for testing."""
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False), patch(
            "application_sdk.clients.redis.REDIS_PASSWORD", "test_password"
        ), patch("application_sdk.clients.redis.REDIS_HOST", "localhost"), patch(
            "application_sdk.clients.redis.REDIS_PORT", "6379"
        ):
            return RedisClient()

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", True)
    async def test_connect_locking_disabled(self, redis_client):
        """Test connection when locking is disabled."""
        await redis_client._connect()
        assert redis_client.redis_client is None

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "test_password")
    @patch("application_sdk.clients.redis.REDIS_HOST", "localhost")
    @patch("application_sdk.clients.redis.REDIS_PORT", "6379")
    @patch("application_sdk.clients.redis.redis.Redis")
    async def test_connect_standalone_success(self, mock_redis_class):
        """Test successful standalone Redis connection."""
        # Arrange
        mock_redis_instance = AsyncMock()
        mock_redis_class.return_value = mock_redis_instance
        redis_client = RedisClient()

        # Act
        await redis_client._connect()

        # Assert
        assert redis_client.redis_client == mock_redis_instance
        mock_redis_instance.ping.assert_called_once()
        mock_redis_class.assert_called_once_with(
            host="localhost", port=6379, password="test_password"
        )

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "test_password")
    @patch("application_sdk.clients.redis.REDIS_HOST", "localhost")
    @patch("application_sdk.clients.redis.REDIS_PORT", "6379")
    @patch("application_sdk.clients.redis.redis.Redis")
    async def test_connect_standalone_connection_error(self, mock_redis_class):
        """Test standalone Redis connection with ConnectionError."""
        # Arrange
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping.side_effect = ConnectionError("Connection refused")
        mock_redis_class.return_value = mock_redis_instance
        redis_client = RedisClient()

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._connect()
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "server unreachable" in str(exc_info.value)

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "test_password")
    @patch("application_sdk.clients.redis.REDIS_HOST", "localhost")
    @patch("application_sdk.clients.redis.REDIS_PORT", "6379")
    @patch("application_sdk.clients.redis.redis.Redis")
    async def test_connect_standalone_timeout_error(self, mock_redis_class):
        """Test standalone Redis connection with TimeoutError."""
        # Arrange
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping.side_effect = TimeoutError("Operation timed out")
        mock_redis_class.return_value = mock_redis_instance
        redis_client = RedisClient()

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._connect()
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "connection timeout" in str(exc_info.value)

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "test_password")
    @patch("application_sdk.clients.redis.REDIS_HOST", "localhost")
    @patch("application_sdk.clients.redis.REDIS_PORT", "6379")
    @patch("application_sdk.clients.redis.redis.Redis")
    async def test_connect_standalone_redis_error(self, mock_redis_class):
        """Test standalone Redis connection with RedisError."""
        # Arrange
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping.side_effect = RedisError("Authentication failed")
        mock_redis_class.return_value = mock_redis_instance
        redis_client = RedisClient()

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._connect()
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "authentication or protocol error" in str(exc_info.value)

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "test_password")
    @patch(
        "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", "host1:26379,host2:26379"
    )
    @patch("application_sdk.clients.redis.REDIS_SENTINEL_SERVICE_NAME", "mymaster")
    @patch("application_sdk.clients.redis.redis.sentinel.Sentinel")
    async def test_connect_sentinel_success(self, mock_sentinel_class):
        """Test successful Sentinel Redis connection."""
        # Arrange
        mock_sentinel = MagicMock()
        mock_redis_instance = AsyncMock()
        mock_sentinel.master_for.return_value = mock_redis_instance
        mock_sentinel_class.return_value = mock_sentinel
        redis_client = RedisClient()

        # Act
        await redis_client._connect()

        # Assert
        assert redis_client.redis_client == mock_redis_instance
        mock_redis_instance.ping.assert_called_once()
        mock_sentinel_class.assert_called_once_with(
            [("host1", 26379), ("host2", 26379)],
            sentinel_kwargs={"password": "test_password"},
        )
        mock_sentinel.master_for.assert_called_once_with(
            "mymaster", password="test_password"
        )

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "test_password")
    @patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", "invalid_format")
    async def test_connect_sentinel_invalid_format(self):
        """Test Sentinel connection with invalid host format."""
        redis_client = RedisClient()
        with pytest.raises(ClientError) as exc_info:
            await redis_client._connect()
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "Unexpected Redis connection error" in str(exc_info.value)

    async def test_close_success(self, redis_client):
        """Test successful Redis client close."""
        # Arrange
        mock_redis_instance = AsyncMock()
        redis_client.redis_client = mock_redis_instance

        # Act
        await redis_client.close()

        # Assert
        mock_redis_instance.aclose.assert_called_once()
        assert redis_client.redis_client is None

    async def test_close_with_error(self, redis_client):
        """Test Redis client close with error."""
        # Arrange
        mock_redis_instance = AsyncMock()
        mock_redis_instance.aclose.side_effect = Exception("Close failed")
        redis_client.redis_client = mock_redis_instance

        # Act
        await redis_client.close()

        # Assert - Should not raise, just log and clean up
        assert redis_client.redis_client is None

    async def test_context_manager(self, redis_client):
        """Test Redis client as async context manager."""
        with patch.object(redis_client, "_connect") as mock_connect, patch.object(
            redis_client, "close"
        ) as mock_close:
            async with redis_client as client:
                assert client == redis_client
                mock_connect.assert_called_once()

            mock_close.assert_called_once()


class TestRedisClientLockAcquisition:
    """Test cases for Redis client lock acquisition."""

    @pytest.fixture
    def redis_client(self):
        """Create a connected RedisClient instance for testing."""
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False), patch(
            "application_sdk.clients.redis.REDIS_PASSWORD", "test_password"
        ), patch("application_sdk.clients.redis.REDIS_HOST", "localhost"), patch(
            "application_sdk.clients.redis.REDIS_PORT", "6379"
        ):
            client = RedisClient()
            client.redis_client = AsyncMock()
            return client

    async def test_acquire_lock_success(self, redis_client):
        """Test successful lock acquisition."""
        # Arrange
        redis_client.redis_client.set.return_value = True

        # Act
        result = await redis_client._acquire_lock("test:resource:0", "owner123", 60)

        # Assert
        assert result is True
        redis_client.redis_client.set.assert_called_once_with(
            "test:resource:0", "owner123", nx=True, ex=60
        )

    async def test_acquire_lock_already_held(self, redis_client):
        """Test lock acquisition when lock is already held."""
        # Arrange
        redis_client.redis_client.set.return_value = (
            None  # Redis returns None when key exists
        )

        # Act
        result = await redis_client._acquire_lock("test:resource:0", "owner123", 60)

        # Assert
        assert result is False

    async def test_acquire_lock_no_client(self):
        """Test lock acquisition with no Redis client initialized."""
        # Arrange
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False), patch(
            "application_sdk.clients.redis.REDIS_PASSWORD", "test_password"
        ), patch("application_sdk.clients.redis.REDIS_HOST", "localhost"), patch(
            "application_sdk.clients.redis.REDIS_PORT", "6379"
        ):
            client = RedisClient()
            # Don't set redis_client - should be None

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await client._acquire_lock("test:resource:0", "owner123", 60)
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "Redis connection failed" in str(exc_info.value)

    async def test_acquire_lock_connection_error(self, redis_client):
        """Test lock acquisition with Redis connection error."""
        # Arrange
        redis_client.redis_client.set.side_effect = ConnectionError("Connection lost")

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._acquire_lock("test:resource:0", "owner123", 60)
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "connection lost during lock acquisition" in str(exc_info.value)

    async def test_acquire_lock_timeout_error(self, redis_client):
        """Test lock acquisition with Redis timeout error."""
        # Arrange
        redis_client.redis_client.set.side_effect = TimeoutError("Operation timed out")

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._acquire_lock("test:resource:0", "owner123", 60)
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "timeout during lock acquisition" in str(exc_info.value)

    async def test_acquire_lock_redis_error(self, redis_client):
        """Test lock acquisition with Redis error."""
        # Arrange
        redis_client.redis_client.set.side_effect = RedisError("Authentication failed")

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._acquire_lock("test:resource:0", "owner123", 60)
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "Redis error during lock acquisition" in str(exc_info.value)

    async def test_acquire_lock_unexpected_error(self, redis_client):
        """Test lock acquisition with unexpected error."""
        # Arrange
        redis_client.redis_client.set.side_effect = ValueError("Unexpected error")

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._acquire_lock("test:resource:0", "owner123", 60)
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "Unexpected error during lock acquisition" in str(exc_info.value)


class TestRedisClientLockRelease:
    """Test cases for Redis client lock release."""

    @pytest.fixture
    def redis_client(self):
        """Create a connected RedisClient instance for testing."""
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False), patch(
            "application_sdk.clients.redis.REDIS_PASSWORD", "test_password"
        ), patch("application_sdk.clients.redis.REDIS_HOST", "localhost"), patch(
            "application_sdk.clients.redis.REDIS_PORT", "6379"
        ):
            client = RedisClient()
            client.redis_client = AsyncMock()
            return client

    async def test_release_lock_success(self, redis_client):
        """Test successful lock release."""
        # Arrange
        redis_client.redis_client.eval.return_value = 1  # Success

        # Act
        released, result = await redis_client._release_lock(
            "test:resource:0", "owner123"
        )

        # Assert
        assert released is True
        assert result == LockReleaseResult.SUCCESS
        redis_client.redis_client.eval.assert_called_once()

    async def test_release_lock_already_released(self, redis_client):
        """Test lock release when lock was already released (TTL expired)."""
        # Arrange
        redis_client.redis_client.eval.return_value = -1  # Key doesn't exist

        # Act
        released, result = await redis_client._release_lock(
            "test:resource:0", "owner123"
        )

        # Assert
        assert released is True  # Already released is considered success
        assert result == LockReleaseResult.ALREADY_RELEASED

    async def test_release_lock_wrong_owner(self, redis_client):
        """Test lock release with wrong owner."""
        # Arrange
        redis_client.redis_client.eval.return_value = -2  # Wrong owner

        # Act
        released, result = await redis_client._release_lock(
            "test:resource:0", "wrong_owner"
        )

        # Assert
        assert released is False
        assert result == LockReleaseResult.WRONG_OWNER

    async def test_release_lock_unexpected_eval_result(self, redis_client):
        """Test lock release with unexpected eval result."""
        # Arrange
        redis_client.redis_client.eval.return_value = -999  # Unknown result

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._release_lock("test:resource:0", "owner123")
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)

    async def test_release_lock_non_integer_result(self, redis_client):
        """Test lock release with non-integer eval result."""
        # Arrange
        redis_client.redis_client.eval.return_value = "invalid"  # Non-integer

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._release_lock("test:resource:0", "owner123")
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "Unexpected error during lock release" in str(exc_info.value)

    async def test_release_lock_no_client(self):
        """Test lock release with no Redis client initialized."""
        # Arrange
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False), patch(
            "application_sdk.clients.redis.REDIS_PASSWORD", "test_password"
        ), patch("application_sdk.clients.redis.REDIS_HOST", "localhost"), patch(
            "application_sdk.clients.redis.REDIS_PORT", "6379"
        ):
            client = RedisClient()
            # Don't set redis_client - should be None

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await client._release_lock("test:resource:0", "owner123")
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "Redis connection failed" in str(exc_info.value)

    async def test_release_lock_connection_error(self, redis_client):
        """Test lock release with Redis connection error."""
        # Arrange
        redis_client.redis_client.eval.side_effect = ConnectionError("Connection lost")

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._release_lock("test:resource:0", "owner123")
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "connection lost during lock release" in str(exc_info.value)

    async def test_release_lock_timeout_error(self, redis_client):
        """Test lock release with Redis timeout error."""
        # Arrange
        redis_client.redis_client.eval.side_effect = TimeoutError("Operation timed out")

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._release_lock("test:resource:0", "owner123")
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "timeout during lock release" in str(exc_info.value)

    async def test_release_lock_redis_error(self, redis_client):
        """Test lock release with Redis error."""
        # Arrange
        redis_client.redis_client.eval.side_effect = RedisError("Authentication failed")

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._release_lock("test:resource:0", "owner123")
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "Redis error during lock release" in str(exc_info.value)

    async def test_release_lock_unexpected_error(self, redis_client):
        """Test lock release with unexpected error."""
        # Arrange
        redis_client.redis_client.eval.side_effect = ValueError("Unexpected error")

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            await redis_client._release_lock("test:resource:0", "owner123")
        assert "ATLAN-CLIENT-401-00" in str(exc_info.value)
        assert "Unexpected error during lock release" in str(exc_info.value)


class TestLockReleaseResultEnum:
    """Test cases for LockReleaseResult enum."""

    def test_enum_values(self):
        """Test LockReleaseResult enum values."""
        assert LockReleaseResult.SUCCESS.value == "success"
        assert LockReleaseResult.ALREADY_RELEASED.value == "already_released"
        assert LockReleaseResult.WRONG_OWNER.value == "wrong_owner"

    def test_enum_comparison(self):
        """Test LockReleaseResult enum comparison."""
        assert LockReleaseResult.SUCCESS == LockReleaseResult.SUCCESS
        assert LockReleaseResult.SUCCESS != LockReleaseResult.WRONG_OWNER


class TestRedisClientIntegration:
    """Integration tests for Redis client operations."""

    @pytest.fixture
    def redis_client(self):
        """Create a connected RedisClient instance for testing."""
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False), patch(
            "application_sdk.clients.redis.REDIS_PASSWORD", "test_password"
        ), patch("application_sdk.clients.redis.REDIS_HOST", "localhost"), patch(
            "application_sdk.clients.redis.REDIS_PORT", "6379"
        ):
            client = RedisClient()
            client.redis_client = AsyncMock()
            return client

    async def test_acquire_then_release_success_flow(self, redis_client):
        """Test complete acquire -> release flow."""
        # Arrange
        redis_client.redis_client.set.return_value = True
        redis_client.redis_client.eval.return_value = 1  # Success

        # Act - Acquire lock
        acquire_result = await redis_client._acquire_lock(
            "test:integration:0", "integration_owner", 60
        )

        # Act - Release lock
        release_result, release_enum = await redis_client._release_lock(
            "test:integration:0", "integration_owner"
        )

        # Assert
        assert acquire_result is True
        assert release_result is True
        assert release_enum == LockReleaseResult.SUCCESS

        # Verify calls
        redis_client.redis_client.set.assert_called_once_with(
            "test:integration:0", "integration_owner", nx=True, ex=60
        )
        redis_client.redis_client.eval.assert_called_once()

    async def test_acquire_success_release_wrong_owner(self, redis_client):
        """Test acquire success but release with wrong owner."""
        # Arrange
        redis_client.redis_client.set.return_value = True
        redis_client.redis_client.eval.return_value = -2  # Wrong owner

        # Act - Acquire lock
        acquire_result = await redis_client._acquire_lock(
            "test:integration:0", "correct_owner", 60
        )

        # Act - Try to release with wrong owner
        release_result, release_enum = await redis_client._release_lock(
            "test:integration:0", "wrong_owner"
        )

        # Assert
        assert acquire_result is True
        assert release_result is False
        assert release_enum == LockReleaseResult.WRONG_OWNER

    async def test_acquire_failure_no_release_needed(self, redis_client):
        """Test that failed acquisition doesn't require release."""
        # Arrange
        redis_client.redis_client.set.return_value = None  # Lock already held

        # Act - Try to acquire lock (should fail)
        acquire_result = await redis_client._acquire_lock(
            "test:integration:0", "test_owner", 60
        )

        # Assert
        assert acquire_result is False

        # Verify no release was attempted
        redis_client.redis_client.eval.assert_not_called()
