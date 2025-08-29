"""Unit tests for Redis client."""

from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import ConnectionError, RedisError, TimeoutError

from application_sdk.clients.redis import LockReleaseResult, RedisClient
from application_sdk.common.error_codes import ClientError


class TestRedisClientConfiguration:
    """Test Redis client configuration and initialization."""

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", True)
    def test_locking_disabled(self):
        """Test client when locking is disabled."""
        client = RedisClient()
        assert client.redis_client is None

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", None)
    def test_missing_password_raises_error(self):
        """Test initialization fails without password."""
        with pytest.raises(ClientError) as exc_info:
            RedisClient()
        assert "ATLAN-CLIENT-403-00" in str(exc_info.value)

    @patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False)
    @patch("application_sdk.clients.redis.REDIS_PASSWORD", "password")
    @patch("application_sdk.clients.redis.REDIS_HOST", "localhost")
    @patch("application_sdk.clients.redis.REDIS_PORT", "6379")
    def test_valid_configuration_succeeds(self):
        """Test initialization succeeds with valid config."""
        client = RedisClient()
        assert client.redis_client is None  # Not connected until _connect() called


class TestRedisClientConnection:
    """Test Redis connection and context management."""

    @pytest.fixture
    def redis_client(self):
        """Create a configured RedisClient for testing."""
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False), patch(
            "application_sdk.clients.redis.REDIS_PASSWORD", "password"
        ), patch("application_sdk.clients.redis.REDIS_HOST", "localhost"), patch(
            "application_sdk.clients.redis.REDIS_PORT", "6379"
        ):
            return RedisClient()

    async def test_context_manager(self, redis_client):
        """Test async context manager functionality."""
        with patch.object(redis_client, "_connect") as mock_connect, patch.object(
            redis_client, "close"
        ) as mock_close:
            async with redis_client as client:
                assert client == redis_client
                mock_connect.assert_called_once()
            mock_close.assert_called_once()


class TestRedisClientLockOperations:
    """Test Redis lock acquisition and release operations."""

    @pytest.fixture
    def redis_client(self):
        """Create a connected RedisClient for testing."""
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False), patch(
            "application_sdk.clients.redis.REDIS_PASSWORD", "password"
        ), patch("application_sdk.clients.redis.REDIS_HOST", "localhost"), patch(
            "application_sdk.clients.redis.REDIS_PORT", "6379"
        ):
            client = RedisClient()
            client.redis_client = AsyncMock()
            return client

    async def test_acquire_lock_success(self, redis_client):
        """Test successful lock acquisition."""
        redis_client.redis_client.set.return_value = True

        result = await redis_client._acquire_lock("resource", "owner", 60)

        assert result is True
        redis_client.redis_client.set.assert_called_once_with(
            "resource", "owner", nx=True, ex=60
        )

    async def test_acquire_lock_already_held(self, redis_client):
        """Test lock acquisition when already held."""
        redis_client.redis_client.set.return_value = None

        result = await redis_client._acquire_lock("resource", "owner", 60)

        assert result is False

    async def test_acquire_lock_error_handling(self, redis_client):
        """Test lock acquisition error handling."""
        redis_client.redis_client.set.side_effect = ConnectionError("Connection failed")

        with pytest.raises(ClientError) as exc_info:
            await redis_client._acquire_lock("resource", "owner", 60)
        assert "ATLAN-CLIENT-503-00" in str(exc_info.value)

    async def test_release_lock_success(self, redis_client):
        """Test successful lock release."""
        redis_client.redis_client.eval.return_value = 1

        released, result = await redis_client._release_lock("resource", "owner")

        assert released is True
        assert result == LockReleaseResult.SUCCESS

    async def test_release_lock_wrong_owner(self, redis_client):
        """Test lock release with wrong owner."""
        redis_client.redis_client.eval.return_value = -2

        released, result = await redis_client._release_lock("resource", "wrong_owner")

        assert released is False
        assert result == LockReleaseResult.WRONG_OWNER

    async def test_release_lock_already_released(self, redis_client):
        """Test lock release when already released."""
        redis_client.redis_client.eval.return_value = -1

        released, result = await redis_client._release_lock("resource", "owner")

        assert released is True
        assert result == LockReleaseResult.ALREADY_RELEASED

    async def test_release_lock_error_handling(self, redis_client):
        """Test lock release error handling."""
        redis_client.redis_client.eval.side_effect = TimeoutError("Timeout")

        with pytest.raises(ClientError) as exc_info:
            await redis_client._release_lock("resource", "owner")
        assert "ATLAN-CLIENT-408-00" in str(exc_info.value)

    async def test_complete_lock_cycle(self, redis_client):
        """Test complete acquire -> release cycle."""
        # Setup successful acquire and release
        redis_client.redis_client.set.return_value = True
        redis_client.redis_client.eval.return_value = 1

        # Acquire lock
        acquired = await redis_client._acquire_lock("resource", "owner", 60)
        assert acquired is True

        # Release lock
        released, result = await redis_client._release_lock("resource", "owner")
        assert released is True
        assert result == LockReleaseResult.SUCCESS


class TestRedisErrorHandling:
    """Test Redis error handling helper function."""

    def test_handle_connection_error(self):
        """Test handling of Redis connection errors."""
        from application_sdk.clients.redis import _handle_redis_error

        with pytest.raises(ClientError) as exc_info:
            _handle_redis_error(ConnectionError("Connection failed"))
        assert "ATLAN-CLIENT-503-00" in str(exc_info.value)
        assert "Connection failed" in str(exc_info.value)

    def test_handle_timeout_error(self):
        """Test handling of Redis timeout errors."""
        from application_sdk.clients.redis import _handle_redis_error

        with pytest.raises(ClientError) as exc_info:
            _handle_redis_error(TimeoutError("Operation timed out"))
        assert "ATLAN-CLIENT-408-00" in str(exc_info.value)
        assert "Operation timed out" in str(exc_info.value)

    def test_handle_redis_protocol_error(self):
        """Test handling of Redis protocol errors."""
        from application_sdk.clients.redis import _handle_redis_error

        with pytest.raises(ClientError) as exc_info:
            _handle_redis_error(RedisError("Protocol error"))
        assert "ATLAN-CLIENT-502-00" in str(exc_info.value)
        assert "Protocol error" in str(exc_info.value)

    def test_handle_generic_error(self):
        """Test handling of generic errors."""
        from application_sdk.clients.redis import _handle_redis_error

        with pytest.raises(ClientError) as exc_info:
            _handle_redis_error(ValueError("Some other error"))
        assert "ATLAN-CLIENT-503-00" in str(exc_info.value)
        assert "Some other error" in str(exc_info.value)


class TestRedisClientSyncContextManager:
    """Test Redis sync context manager functionality."""

    @pytest.fixture
    def redis_client(self):
        """Create a configured RedisClient for testing."""
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False), patch(
            "application_sdk.clients.redis.REDIS_PASSWORD", "password"
        ), patch("application_sdk.clients.redis.REDIS_HOST", "localhost"), patch(
            "application_sdk.clients.redis.REDIS_PORT", "6379"
        ):
            return RedisClient()

    def test_sync_context_manager(self, redis_client):
        """Test sync context manager functionality."""
        with patch.object(redis_client, "_connect_sync") as mock_connect, patch.object(
            redis_client, "close_sync"
        ) as mock_close:
            with redis_client as client:
                assert client == redis_client
                mock_connect.assert_called_once()
            mock_close.assert_called_once()


class TestLockReleaseResultEnum:
    """Test LockReleaseResult enum."""

    def test_enum_values(self):
        """Test enum has correct values."""
        assert LockReleaseResult.SUCCESS.value == "success"
        assert LockReleaseResult.ALREADY_RELEASED.value == "already_released"
        assert LockReleaseResult.WRONG_OWNER.value == "wrong_owner"
