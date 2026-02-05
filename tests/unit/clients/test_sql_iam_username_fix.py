"""
Test suite for validating the IAM username fix in SQL client.

This test validates the fix for the bug where IAM User authentication was
incorrectly using AWS Access Key ID as the SQLAlchemy username instead of
the actual database username stored in credentials.extra.username.

Root Cause:
-----------
For IAM User authentication with PostgreSQL:
- credentials.username = AWS Access Key ID (for AWS IAM token generation)
- credentials.password = AWS Secret Access Key
- extra.username = Actual Database Username (what PostgreSQL needs)

The bug occurred because the original code checked credentials first:
    value = self.credentials.get(param) or extra.get(param)

This caused credentials.username (AWS Access Key ID) to be used instead of
extra.username (the actual DB username) in the connection string.

The fix reverses the priority:
    value = extra.get(param) or self.credentials.get(param)

This ensures extra.username is used when available, falling back to
credentials.username only when extra doesn't have the value.
"""

import pytest
from unittest.mock import patch, MagicMock
from typing import Dict, Any

from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import BaseSQLClient, AsyncBaseSQLClient


class MockPostgresClient(BaseSQLClient):
    """Mock PostgreSQL client for testing with IAM authentication."""

    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        defaults={
            "connect_timeout": 5,
            "application_name": "Atlan",
        },
    )


class MockAsyncPostgresClient(AsyncBaseSQLClient):
    """Mock Async PostgreSQL client for testing with IAM authentication."""

    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        defaults={
            "connect_timeout": 5,
            "application_name": "Atlan",
        },
    )


class TestIAMUserAuthenticationFix:
    """Test IAM User authentication correctly uses extra.username instead of credentials.username."""

    @pytest.fixture
    def iam_user_credentials(self) -> Dict[str, Any]:
        """
        IAM User credentials as defined in postgres.yaml (lines 587-664).

        Structure:
        - username: AWS Access Key ID (used for IAM token generation)
        - password: AWS Secret Access Key (used for IAM token generation)
        - extra.username: Actual database username (should be used in connection string)
        - extra.database: Database name
        """
        return {
            "authType": "iam_user",
            "host": "my-database.cluster-xxxxx.us-east-1.rds.amazonaws.com",
            "port": 5432,
            "username": "AKIAIOSFODNN7EXAMPLE",  # AWS Access Key ID
            "password": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # AWS Secret Key
            "extra": {
                "username": "db_admin_user",  # Actual DB username - THIS should be used
                "database": "production_db",
            },
        }

    @pytest.fixture
    def mock_iam_token(self) -> str:
        """Mock IAM authentication token."""
        return "mock-iam-auth-token-12345"

    def test_iam_user_connection_string_uses_extra_username(
        self, iam_user_credentials: Dict[str, Any], mock_iam_token: str
    ):
        """
        Test that IAM User authentication uses extra.username (DB username)
        instead of credentials.username (AWS Access Key ID).

        Expected behavior:
        - Connection string should contain 'db_admin_user' (extra.username)
        - Connection string should NOT contain 'AKIAIOSFODNN7EXAMPLE' (AWS Access Key)
        """
        client = MockPostgresClient()
        client.credentials = iam_user_credentials

        with patch.object(client, "get_iam_user_token", return_value=mock_iam_token):
            connection_string = client.get_sqlalchemy_connection_string()

        # Validate correct username is used
        assert "db_admin_user" in connection_string, (
            f"Connection string should contain DB username 'db_admin_user', "
            f"got: {connection_string}"
        )

        # Validate AWS Access Key is NOT used as username
        assert "AKIAIOSFODNN7EXAMPLE" not in connection_string.split("@")[0], (
            f"Connection string should NOT contain AWS Access Key ID as username, "
            f"got: {connection_string}"
        )

        # Validate complete connection string format
        expected_start = "postgresql+psycopg://db_admin_user:"
        assert connection_string.startswith(expected_start), (
            f"Connection string should start with '{expected_start}', "
            f"got: {connection_string}"
        )

    def test_async_iam_user_connection_string_uses_extra_username(
        self, iam_user_credentials: Dict[str, Any], mock_iam_token: str
    ):
        """
        Test that AsyncBaseSQLClient also correctly uses extra.username for IAM User auth.
        
        AsyncBaseSQLClient inherits from BaseSQLClient and uses the same
        get_sqlalchemy_connection_string method, so the fix should apply to both.
        """
        client = MockAsyncPostgresClient()
        client.credentials = iam_user_credentials

        with patch.object(client, "get_iam_user_token", return_value=mock_iam_token):
            connection_string = client.get_sqlalchemy_connection_string()

        assert "db_admin_user" in connection_string
        assert "AKIAIOSFODNN7EXAMPLE" not in connection_string.split("@")[0]


class TestIAMRoleAuthenticationNoRegression:
    """Test IAM Role authentication still works correctly after the fix."""

    @pytest.fixture
    def iam_role_credentials(self) -> Dict[str, Any]:
        """
        IAM Role credentials as defined in postgres.yaml (lines 665-741).

        Structure:
        - username: Database username (NOT an AWS credential)
        - extra.aws_role_arn: AWS Role ARN for assuming role
        - extra.aws_external_id: Optional external ID
        - extra.database: Database name
        """
        return {
            "authType": "iam_role",
            "host": "my-database.cluster-xxxxx.us-east-1.rds.amazonaws.com",
            "port": 5432,
            "username": "iam_db_user",  # This IS the DB username for IAM role
            "extra": {
                "aws_role_arn": "arn:aws:iam::123456789012:role/RDSAccessRole",
                "aws_external_id": "external-id-12345",
                "database": "production_db",
            },
        }

    @pytest.fixture
    def mock_iam_token(self) -> str:
        """Mock IAM authentication token."""
        return "mock-iam-role-token-67890"

    def test_iam_role_connection_string_uses_credentials_username(
        self, iam_role_credentials: Dict[str, Any], mock_iam_token: str
    ):
        """
        Test that IAM Role authentication correctly uses credentials.username.

        For IAM Role auth, extra.username is NOT set, so credentials.username
        should be used as the database username.
        """
        client = MockPostgresClient()
        client.credentials = iam_role_credentials

        with patch.object(client, "get_iam_role_token", return_value=mock_iam_token):
            connection_string = client.get_sqlalchemy_connection_string()

        # Validate correct username is used
        assert "iam_db_user" in connection_string, (
            f"Connection string should contain DB username 'iam_db_user', "
            f"got: {connection_string}"
        )

        expected_start = "postgresql+psycopg://iam_db_user:"
        assert connection_string.startswith(expected_start), (
            f"Connection string should start with '{expected_start}', "
            f"got: {connection_string}"
        )

    def test_async_iam_role_connection_string_uses_credentials_username(
        self, iam_role_credentials: Dict[str, Any], mock_iam_token: str
    ):
        """Test AsyncBaseSQLClient also correctly handles IAM Role auth."""
        client = MockAsyncPostgresClient()
        client.credentials = iam_role_credentials

        with patch.object(client, "get_iam_role_token", return_value=mock_iam_token):
            connection_string = client.get_sqlalchemy_connection_string()

        assert "iam_db_user" in connection_string
        expected_start = "postgresql+psycopg://iam_db_user:"
        assert connection_string.startswith(expected_start)


class TestBasicAuthenticationNoRegression:
    """Test Basic authentication still works correctly after the fix."""

    @pytest.fixture
    def basic_credentials(self) -> Dict[str, Any]:
        """
        Basic auth credentials as defined in postgres.yaml (lines 538-586).

        Structure:
        - username: Database username
        - password: Database password
        - extra.database: Database name
        """
        return {
            "authType": "basic",
            "host": "localhost",
            "port": 5432,
            "username": "postgres_admin",
            "password": "super_secret_password",
            "extra": {
                "database": "my_database",
            },
        }

    def test_basic_auth_connection_string_uses_credentials_username(
        self, basic_credentials: Dict[str, Any]
    ):
        """
        Test that Basic authentication correctly uses credentials.username.

        For Basic auth, extra.username is NOT set, so credentials.username
        should be used as the database username.
        """
        client = MockPostgresClient()
        client.credentials = basic_credentials

        connection_string = client.get_sqlalchemy_connection_string()

        # Validate correct username is used
        assert "postgres_admin" in connection_string, (
            f"Connection string should contain username 'postgres_admin', "
            f"got: {connection_string}"
        )

        # Validate complete connection string structure
        expected_start = "postgresql+psycopg://postgres_admin:"
        assert connection_string.startswith(expected_start), (
            f"Connection string should start with '{expected_start}', "
            f"got: {connection_string}"
        )

    def test_async_basic_auth_connection_string(self, basic_credentials: Dict[str, Any]):
        """Test AsyncBaseSQLClient also correctly handles Basic auth."""
        client = MockAsyncPostgresClient()
        client.credentials = basic_credentials

        connection_string = client.get_sqlalchemy_connection_string()

        assert "postgres_admin" in connection_string
        expected_start = "postgresql+psycopg://postgres_admin:"
        assert connection_string.startswith(expected_start)


class TestExtraParameterPriority:
    """Test that extra parameters always take priority over credentials parameters."""

    @pytest.fixture
    def credentials_with_conflicting_values(self) -> Dict[str, Any]:
        """
        Credentials where both extra and root have the same parameter name.
        extra values should take priority.
        """
        return {
            "authType": "basic",
            "host": "credentials-host.example.com",
            "port": 5432,
            "username": "credentials_username",
            "password": "credentials_password",
            "extra": {
                "host": "extra-host.example.com",  # Should be used
                "port": 5433,  # Should be used
                "username": "extra_username",  # Should be used
                "database": "my_database",
            },
        }

    def test_extra_parameters_take_priority(
        self, credentials_with_conflicting_values: Dict[str, Any]
    ):
        """
        Test that when both extra and credentials have the same parameter,
        extra value takes priority.
        """
        client = MockPostgresClient()
        client.credentials = credentials_with_conflicting_values

        connection_string = client.get_sqlalchemy_connection_string()

        # extra.username should be used
        assert "extra_username" in connection_string, (
            f"extra.username should be used, got: {connection_string}"
        )
        assert "credentials_username" not in connection_string.split("@")[0], (
            f"credentials.username should NOT be used, got: {connection_string}"
        )

        # extra.host should be used
        assert "extra-host.example.com" in connection_string, (
            f"extra.host should be used, got: {connection_string}"
        )

        # extra.port should be used
        assert ":5433/" in connection_string, (
            f"extra.port (5433) should be used, got: {connection_string}"
        )


class TestEdgeCases:
    """Test edge cases for the parameter resolution logic."""

    def test_empty_extra_username_falls_back_to_credentials(self):
        """Test that empty string in extra falls back to credentials."""
        credentials = {
            "authType": "basic",
            "host": "localhost",
            "port": 5432,
            "username": "fallback_user",
            "password": "password",
            "extra": {
                "username": "",  # Empty string should fall back
                "database": "test_db",
            },
        }

        client = MockPostgresClient()
        client.credentials = credentials

        connection_string = client.get_sqlalchemy_connection_string()

        # Should fall back to credentials.username since extra.username is empty
        assert "fallback_user" in connection_string, (
            f"Should fall back to credentials.username, got: {connection_string}"
        )

    def test_none_extra_username_falls_back_to_credentials(self):
        """Test that None in extra falls back to credentials."""
        credentials = {
            "authType": "basic",
            "host": "localhost",
            "port": 5432,
            "username": "fallback_user",
            "password": "password",
            "extra": {
                "username": None,  # None should fall back
                "database": "test_db",
            },
        }

        client = MockPostgresClient()
        client.credentials = credentials

        connection_string = client.get_sqlalchemy_connection_string()

        # Should fall back to credentials.username since extra.username is None
        assert "fallback_user" in connection_string, (
            f"Should fall back to credentials.username, got: {connection_string}"
        )

    def test_missing_extra_key_falls_back_to_credentials(self):
        """Test that missing key in extra falls back to credentials."""
        credentials = {
            "authType": "basic",
            "host": "localhost",
            "port": 5432,
            "username": "fallback_user",
            "password": "password",
            "extra": {
                # No username key at all
                "database": "test_db",
            },
        }

        client = MockPostgresClient()
        client.credentials = credentials

        connection_string = client.get_sqlalchemy_connection_string()

        # Should fall back to credentials.username since extra doesn't have username
        assert "fallback_user" in connection_string, (
            f"Should fall back to credentials.username, got: {connection_string}"
        )
