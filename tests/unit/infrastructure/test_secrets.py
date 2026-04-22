"""Unit tests for MockSecretStore, EnvironmentSecretStore, and get_deployment_secret."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.infrastructure.secrets import (
    EnvironmentSecretStore,
    SecretNotFoundError,
    SecretStoreError,
    get_deployment_secret,
)
from application_sdk.testing.mocks import MockSecretStore


class TestMockSecretStore:
    """Tests for MockSecretStore."""

    async def test_get_returns_secret_value(self) -> None:
        """Test that get returns the stored secret value."""
        store = MockSecretStore({"MY_SECRET": "hunter2"})

        value = await store.get("MY_SECRET")

        assert value == "hunter2"

    async def test_get_raises_secret_not_found_when_missing(self) -> None:
        """Test that get raises SecretNotFoundError when secret is not found."""
        store = MockSecretStore()

        with pytest.raises(SecretNotFoundError) as exc_info:
            await store.get("MISSING")

        assert "MISSING" in str(exc_info.value)

    async def test_get_optional_returns_value_when_present(self) -> None:
        """Test that get_optional returns the value when the secret exists."""
        store = MockSecretStore({"TOKEN": "abc123"})

        value = await store.get_optional("TOKEN")

        assert value == "abc123"

    async def test_get_optional_returns_none_when_missing(self) -> None:
        """Test that get_optional returns None when the secret is not found."""
        store = MockSecretStore()

        value = await store.get_optional("MISSING")

        assert value is None

    async def test_get_bulk_returns_found_secrets(self) -> None:
        """Test that get_bulk returns only the secrets that exist."""
        store = MockSecretStore({"A": "val_a", "B": "val_b", "C": "val_c"})

        result = await store.get_bulk(["A", "C", "D"])

        assert result == {"A": "val_a", "C": "val_c"}

    async def test_get_bulk_returns_empty_when_none_found(self) -> None:
        """Test that get_bulk returns empty dict when no secrets are found."""
        store = MockSecretStore({"X": "x"})

        result = await store.get_bulk(["A", "B"])

        assert result == {}

    async def test_list_names_returns_all_names(self) -> None:
        """Test that list_names returns all secret names."""
        store = MockSecretStore({"FOO": "1", "BAR": "2"})

        names = await store.list_names()

        assert sorted(names) == ["BAR", "FOO"]

    async def test_list_names_empty_store(self) -> None:
        """Test that list_names returns empty list for empty store."""
        store = MockSecretStore()

        names = await store.list_names()

        assert names == []

    def test_set_adds_secret(self) -> None:
        """Test that set() adds a secret to the store."""
        store = MockSecretStore()
        store.set("NEW_SECRET", "new_value")

        assert store._secrets["NEW_SECRET"] == "new_value"

    def test_clear_removes_all_secrets(self) -> None:
        """Test that clear() removes all secrets."""
        store = MockSecretStore({"A": "1", "B": "2"})
        store.clear()

        assert store._secrets == {}

    async def test_init_with_no_args_creates_empty_store(self) -> None:
        """Test that initializing without args creates an empty store."""
        store = MockSecretStore()

        names = await store.list_names()
        assert names == []

    async def test_init_with_secrets_dict(self) -> None:
        """Test that initializing with a secrets dict populates the store."""
        secrets = {"DB_PASS": "secret123", "API_KEY": "key456"}
        store = MockSecretStore(secrets)

        assert await store.get("DB_PASS") == "secret123"
        assert await store.get("API_KEY") == "key456"


class TestEnvironmentSecretStore:
    """Tests for EnvironmentSecretStore."""

    async def test_get_reads_from_environ(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that get reads the secret from environment variables."""
        monkeypatch.setenv("MY_VAR", "my_value")
        store = EnvironmentSecretStore()

        value = await store.get("MY_VAR")

        assert value == "my_value"

    async def test_get_raises_not_found_when_env_missing(self) -> None:
        """Test that get raises SecretNotFoundError when env var is not set."""
        store = EnvironmentSecretStore()
        # Use an env var name unlikely to be set
        key = "VERY_UNLIKELY_TEST_SECRET_XYZ_12345"
        os.environ.pop(key, None)

        with pytest.raises(SecretNotFoundError):
            await store.get(key)

    async def test_get_with_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that prefix is prepended to the env var name."""
        monkeypatch.setenv("APP_DB_PASS", "secret")
        store = EnvironmentSecretStore(prefix="APP_")

        value = await store.get("DB_PASS")

        assert value == "secret"

    async def test_get_optional_returns_none_when_missing(self) -> None:
        """Test that get_optional returns None when env var is not set."""
        store = EnvironmentSecretStore()
        key = "VERY_UNLIKELY_TEST_SECRET_XYZ_12345"
        os.environ.pop(key, None)

        value = await store.get_optional(key)

        assert value is None

    async def test_get_optional_returns_value_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that get_optional returns the value when env var is set."""
        monkeypatch.setenv("MY_TOKEN", "tok123")
        store = EnvironmentSecretStore()

        value = await store.get_optional("MY_TOKEN")

        assert value == "tok123"

    async def test_get_bulk_returns_found_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that get_bulk returns only secrets present in environment."""
        monkeypatch.setenv("SECRET_A", "a_value")
        monkeypatch.setenv("SECRET_C", "c_value")
        store = EnvironmentSecretStore()

        result = await store.get_bulk(["SECRET_A", "SECRET_B", "SECRET_C"])

        assert result["SECRET_A"] == "a_value"
        assert result["SECRET_C"] == "c_value"
        assert "SECRET_B" not in result

    async def test_get_bulk_with_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that get_bulk applies prefix to env var lookups."""
        monkeypatch.setenv("MYAPP_HOST", "localhost")
        monkeypatch.setenv("MYAPP_PORT", "5432")
        store = EnvironmentSecretStore(prefix="MYAPP_")

        result = await store.get_bulk(["HOST", "PORT", "MISSING"])

        assert result == {"HOST": "localhost", "PORT": "5432"}

    async def test_list_names_with_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that list_names returns keys matching the prefix (without prefix)."""
        # Clear any existing vars with this prefix and set known ones
        monkeypatch.setenv("TESTPFX_KEY1", "v1")
        monkeypatch.setenv("TESTPFX_KEY2", "v2")
        store = EnvironmentSecretStore(prefix="TESTPFX_")

        names = await store.list_names()

        assert "KEY1" in names
        assert "KEY2" in names

    async def test_list_names_no_prefix_returns_all_env_keys(self) -> None:
        """Test that list_names without prefix returns all environment variable names."""
        store = EnvironmentSecretStore()

        names = await store.list_names()

        # Should contain at least some env vars (PATH, HOME, etc.)
        assert len(names) > 0
        # All current env keys should be present
        for key in os.environ:
            assert key in names


class TestSecretStoreError:
    """Tests for SecretStoreError and SecretNotFoundError."""

    def test_secret_store_error_includes_code(self) -> None:
        """Test that SecretStoreError includes error code in string."""
        err = SecretStoreError("store failed")
        assert "AAF-INF-004" in str(err)

    def test_secret_not_found_error_includes_code(self) -> None:
        """Test that SecretNotFoundError includes error code in string."""
        err = SecretNotFoundError("MY_SECRET")
        assert "AAF-INF-005" in str(err)
        assert "MY_SECRET" in str(err)

    def test_secret_not_found_is_subclass_of_secret_store_error(self) -> None:
        """Test that SecretNotFoundError is a subclass of SecretStoreError."""
        assert issubclass(SecretNotFoundError, SecretStoreError)

    def test_secret_store_error_with_cause(self) -> None:
        """Test that cause is included in string representation."""
        cause = ConnectionError("db unreachable")
        err = SecretStoreError("failed", cause=cause)
        assert "ConnectionError" in str(err)

    def test_secret_store_error_with_secret_name(self) -> None:
        """Test that secret_name is included in string representation."""
        err = SecretStoreError("failed", secret_name="MY_VAR")
        assert "MY_VAR" in str(err)


class TestGetDeploymentSecret:
    """Tests for get_deployment_secret()."""

    async def test_returns_none_in_local_environment(self) -> None:
        """Local environment → returns None without touching Dapr."""
        with (
            patch("application_sdk.constants.DEPLOYMENT_NAME", "local"),
            patch("application_sdk.constants.LOCAL_ENVIRONMENT", "local"),
        ):
            result = await get_deployment_secret("any_key")

        assert result is None

    async def test_returns_none_when_client_fails(self) -> None:
        """AsyncDaprClient construction fails → returns None."""
        with (
            patch("application_sdk.constants.DEPLOYMENT_NAME", "prod"),
            patch("application_sdk.constants.LOCAL_ENVIRONMENT", "local"),
            patch(
                "application_sdk.constants.DEPLOYMENT_SECRET_STORE_NAME",
                "deployment-secret-store",
            ),
            patch(
                "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
                side_effect=Exception("not found"),
            ),
        ):
            result = await get_deployment_secret("MY_KEY")

        assert result is None

    async def test_returns_value_from_multi_key_bundle(self) -> None:
        """Multi-key bundle contains the requested key → returns its value."""
        bundle = {"DB_HOST": "db.example.com", "DB_PORT": "5432"}
        mock_client = MagicMock(
            get_secret=AsyncMock(
                return_value={"ATLAN_DEPLOYMENT_SECRETS": json.dumps(bundle)}
            ),
            close=AsyncMock(),
        )

        with (
            patch("application_sdk.constants.DEPLOYMENT_NAME", "prod"),
            patch("application_sdk.constants.LOCAL_ENVIRONMENT", "local"),
            patch(
                "application_sdk.constants.DEPLOYMENT_SECRET_STORE_NAME",
                "deployment-secret-store",
            ),
            patch(
                "application_sdk.constants.DEPLOYMENT_SECRET_PATH",
                "ATLAN_DEPLOYMENT_SECRETS",
            ),
            patch(
                "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
                return_value=mock_client,
            ),
        ):
            result = await get_deployment_secret("DB_HOST")

        assert result == "db.example.com"

    async def test_falls_back_to_single_key_when_bundle_misses(self) -> None:
        """Bundle doesn't contain the key → falls back to single-key lookup."""
        mock_client = MagicMock(
            get_secret=AsyncMock(
                side_effect=[
                    {
                        "ATLAN_DEPLOYMENT_SECRETS": json.dumps(
                            {"OTHER_KEY": "other_value"}
                        )
                    },
                    {"DB_PORT": "5432"},
                ]
            ),
            close=AsyncMock(),
        )

        with (
            patch("application_sdk.constants.DEPLOYMENT_NAME", "prod"),
            patch("application_sdk.constants.LOCAL_ENVIRONMENT", "local"),
            patch(
                "application_sdk.constants.DEPLOYMENT_SECRET_STORE_NAME",
                "deployment-secret-store",
            ),
            patch(
                "application_sdk.constants.DEPLOYMENT_SECRET_PATH",
                "ATLAN_DEPLOYMENT_SECRETS",
            ),
            patch(
                "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
                return_value=mock_client,
            ),
        ):
            result = await get_deployment_secret("DB_PORT")

        assert result == "5432"

    async def test_returns_none_on_exception(self) -> None:
        """Any unexpected exception → returns None (never raises)."""
        with (
            patch("application_sdk.constants.DEPLOYMENT_NAME", "prod"),
            patch("application_sdk.constants.LOCAL_ENVIRONMENT", "local"),
            patch(
                "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
                side_effect=RuntimeError("Dapr unavailable"),
            ),
        ):
            result = await get_deployment_secret("ANY_KEY")

        assert result is None
