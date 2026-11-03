"""Unit tests for MockSecretStore, EnvironmentSecretStore, and get_deployment_secret."""

import json
import os
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import (
    ColdStartRaceError,
    DaprSidecarUnreachableError,
)
from application_sdk.infrastructure.secrets import (
    EnvironmentSecretStore,
    SecretNotFoundError,
    SecretStoreError,
    SecretStoreNotConfiguredError,
    SecretStoreUnavailableError,
    SecretStoreUnreachableError,
    get_deployment_secret,
)
from application_sdk.testing.mocks import MockSecretStore


class TestSecretStoreUnreachableError:
    """The terminal secret-store outage type — distinct from the transient
    SecretStoreUnavailableError, catchable by both hierarchies, and secret-safe."""

    def test_multiply_inherits_both_hierarchies(self) -> None:
        err = SecretStoreUnreachableError("sha256:abcd1234")
        # Caught by the domain umbrella AND the cold-start marker chain.
        assert isinstance(err, SecretStoreError)
        assert isinstance(err, DaprSidecarUnreachableError)
        assert isinstance(err, ColdStartRaceError)
        # Distinct from the transient sibling, not a subtype of it.
        assert not isinstance(err, SecretStoreUnavailableError)

    def test_terminal_code_and_category(self) -> None:
        err = SecretStoreUnreachableError("sha256:abcd1234")
        assert err.code == "DEPENDENCY_UNAVAILABLE_SIDECAR_UNREACHABLE"
        assert err.category == FailureCategory.DEPENDENCY_UNAVAILABLE

    def test_carries_secret_free_diagnostics(self) -> None:
        err = SecretStoreUnreachableError(
            "sha256:abcd1234",
            component="secretstore",
            attempts=2,
            elapsed_seconds=120.0,
        )
        assert err.component == "secretstore"
        assert err.attempts == 2
        assert err.elapsed_seconds == 120.0

    def test_is_secret_safe_no_ref_key_no_cause(self) -> None:
        # secret_name is the hash label, never the raw ref-key; no cause chain
        # that could re-embed the ref-key via a percent-encoded URL.
        err = SecretStoreUnreachableError("sha256:abcd1234")
        assert "sha256:abcd1234" in str(err)
        assert err.secret_name == "sha256:abcd1234"
        assert err.cause is None

    def test_diagnostics_reach_the_wire_evidence(self) -> None:
        # The whole point of the fields is operator-visible diagnostics, so they
        # must survive to_failure_details() (the class must be a @dataclass, or
        # dataclasses.fields() would omit the plain-attribute fields).
        err = SecretStoreUnreachableError(
            "sha256:abcd1234",
            component="secretstore",
            attempts=2,
            elapsed_seconds=120.0,
        )
        evidence = err.to_failure_details().evidence
        assert evidence["component"] == "secretstore"
        assert evidence["attempts"] == 2
        assert evidence["elapsed_seconds"] == 120.0
        # Redaction still holds on the wire: the hashed label, never a raw ref-key.
        assert evidence["secret_name"] == "sha256:abcd1234"

    def test_stays_wire_retryable_by_design(self) -> None:
        # Deliberate: naming the terminal outage does not stop the retry. An
        # activity-level retry can still recover on a healthy worker, so the type
        # stays retryable (the transient sibling's behaviour is unchanged).
        # Flipping this is the deferred "stop on exhaustion" policy decision.
        assert (
            SecretStoreUnreachableError("sha256:abcd1234")
            .to_failure_details()
            .retryable
            is True
        )


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

    def test_secret_not_found_is_app_error(self) -> None:
        """SecretNotFoundError and SecretStoreError are independent AppError leaves."""
        from application_sdk.errors.base import AppError
        from application_sdk.errors.leaves import (
            DependencyUnavailableError,
            NotFoundError,
        )

        assert issubclass(SecretNotFoundError, NotFoundError)
        assert issubclass(SecretStoreError, DependencyUnavailableError)
        assert issubclass(SecretNotFoundError, AppError)
        assert issubclass(SecretStoreError, AppError)

    def test_secret_store_error_with_cause(self) -> None:
        """Test that cause is included in string representation."""
        cause = ConnectionError("db unreachable")
        err = SecretStoreError("failed", cause=cause)
        assert "ConnectionError" in str(err)

    def test_secret_store_error_with_secret_name(self) -> None:
        """Test that secret_name is included in string representation."""
        err = SecretStoreError("failed", secret_name="MY_VAR")
        assert "MY_VAR" in str(err)

    def test_secret_store_error_defaults_retryable(self) -> None:
        """Unset retryable falls back to DependencyUnavailableError's default (True)."""
        err = SecretStoreError("failed")
        assert err.effective_retryable is True
        assert err.to_failure_details().retryable is True

    def test_secret_store_error_retryable_override(self) -> None:
        """A definitive rejection (e.g. a 4xx) can be marked non-retryable
        explicitly — retrying it would fail identically every time."""
        err = SecretStoreError("forbidden", secret_name="MY_VAR", retryable=False)
        assert err.effective_retryable is False
        assert err.to_failure_details().retryable is False


class TestSecretStoreUnavailableError:
    """Tests for SecretStoreUnavailableError — the structurally-transient
    "store unreachable" subtype callers retry a cold-start race against."""

    def test_is_a_secret_store_error(self) -> None:
        err = SecretStoreUnavailableError("db_pass", cause=RuntimeError("boom"))
        assert isinstance(err, SecretStoreError)

    def test_includes_code_and_secret_name(self) -> None:
        err = SecretStoreUnavailableError("db_pass")
        assert err.code == "DEPENDENCY_UNAVAILABLE_SECRET_STORE_UNREACHABLE"
        assert "db_pass" in str(err)

    def test_defaults_retryable_true(self) -> None:
        """Unlike a definitive rejection, an unreachable store is retryable
        by default — it inherits DependencyUnavailableError's default."""
        err = SecretStoreUnavailableError("db_pass")
        assert err.effective_retryable is True


class TestGetDeploymentSecret:
    """Tests for get_deployment_secret()."""

    async def test_returns_none_in_local_environment(self) -> None:
        """Local environment → returns None without touching Dapr."""
        with (
            patch("application_sdk.constants.DEPLOYMENT_NAME", "local"),
            patch("application_sdk.constants.LOCAL_ENVIRONMENT", "local"),
        ):
            result = await get_deployment_secret("any_key", raise_on_error=False)

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
            result = await get_deployment_secret("MY_KEY", raise_on_error=False)

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
            result = await get_deployment_secret("DB_HOST", raise_on_error=False)

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
            result = await get_deployment_secret("DB_PORT", raise_on_error=False)

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
            result = await get_deployment_secret("ANY_KEY", raise_on_error=False)

        assert result is None


class TestGetDeploymentSecretSingleEntryFallback:
    """The single-key fallback must never return a field of a different secret."""

    async def test_does_not_return_inner_field_of_unwrapped_json_secret(
        self,
    ) -> None:
        # The secret named DB_PORT holds a JSON document without a DB_PORT
        # field. process_secret_data unwraps it to {"user": "admin"}; the
        # single-entry fallback must not hand back "admin" as the port.
        mock_client = MagicMock(
            get_secret=AsyncMock(
                side_effect=[
                    {"ATLAN_DEPLOYMENT_SECRETS": json.dumps({"OTHER": "x"})},
                    {"DB_PORT": json.dumps({"user": "admin"})},
                ]
            ),
            close=AsyncMock(),
        )
        with (
            patch("application_sdk.constants.DEPLOYMENT_NAME", "prod"),
            patch("application_sdk.constants.LOCAL_ENVIRONMENT", "local"),
            patch(
                "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
                return_value=mock_client,
            ),
        ):
            result = await get_deployment_secret("DB_PORT", raise_on_error=False)

        assert result != "admin"
        assert result == json.dumps({"user": "admin"})


def _dapr_secret_error(
    status: int, error_code: str, message: str
) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://localhost:3500/v1.0/secrets/store/k")
    response = httpx.Response(
        status, json={"errorCode": error_code, "message": message}, request=request
    )
    return httpx.HTTPStatusError("dapr error", request=request, response=response)


def _not_found(key: str) -> httpx.HTTPStatusError:
    return _dapr_secret_error(
        500,
        "ERR_SECRET_GET",
        f"failed getting secret with key {key} from secret store "
        f"deployment-secret-store: secret {key} not found",
    )


@pytest.fixture
def _deployed():
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
    ):
        yield


def _client(*responses: object) -> MagicMock:
    return MagicMock(
        get_secret=AsyncMock(side_effect=list(responses)), close=AsyncMock()
    )


def _patch_client(client: MagicMock):
    return patch(
        "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
        return_value=client,
    )


@pytest.mark.usefixtures("_deployed")
class TestGetDeploymentSecretRaiseOnError:
    """raise_on_error=True: typed failures and the cold-start retry."""

    async def test_retries_cold_start_transient_then_returns_value(
        self, fast_dapr_cold_start_retry
    ) -> None:
        client = _client(
            httpx.ConnectError("connection refused"),
            {"ATLAN_DEPLOYMENT_SECRETS": json.dumps({"DB_HOST": "db.example.com"})},
        )
        with _patch_client(client):
            result = await get_deployment_secret("DB_HOST", raise_on_error=True)

        assert result == "db.example.com"
        assert client.get_secret.await_count == 2

    async def test_cold_start_exhausted_raises_unreachable(
        self, deterministic_dapr_cold_start_deadline
    ) -> None:
        client = MagicMock(
            get_secret=AsyncMock(side_effect=httpx.ConnectError("refused")),
            close=AsyncMock(),
        )
        with _patch_client(client), pytest.raises(SecretStoreUnreachableError) as ei:
            await get_deployment_secret("DB_HOST", raise_on_error=True)

        assert ei.value.component == "deployment-secret-store"
        assert ei.value.attempts == 2

    async def test_missing_key_raises_not_found(self) -> None:
        client = _client(
            {"ATLAN_DEPLOYMENT_SECRETS": json.dumps({"OTHER": "x"})},
            _not_found("DB_HOST"),
        )
        with _patch_client(client), pytest.raises(SecretNotFoundError) as ei:
            await get_deployment_secret("DB_HOST", raise_on_error=True)

        assert ei.value.secret_name == "DB_HOST"

    async def test_missing_bundle_falls_through_to_single_key(self) -> None:
        client = _client(_not_found("ATLAN_DEPLOYMENT_SECRETS"), {"DB_HOST": "h"})
        with _patch_client(client):
            result = await get_deployment_secret("DB_HOST", raise_on_error=True)

        assert result == "h"

    async def test_store_failure_raises_store_error_with_cause(self) -> None:
        failure = _dapr_secret_error(
            500,
            "ERR_SECRET_GET",
            "failed getting secret with key ATLAN_DEPLOYMENT_SECRETS from secret "
            "store deployment-secret-store: AccessDeniedException: denied",
        )
        client = _client(failure)
        with _patch_client(client), pytest.raises(SecretStoreError) as ei:
            await get_deployment_secret("DB_HOST", raise_on_error=True)

        assert not isinstance(ei.value, SecretNotFoundError)
        assert ei.value.cause is failure
        # A store failure never reaches the single-key lookup.
        assert client.get_secret.await_count == 1

    async def test_local_environment_raises_not_configured(self) -> None:
        with (
            patch("application_sdk.constants.DEPLOYMENT_NAME", "local"),
            pytest.raises(SecretStoreNotConfiguredError),
        ):
            await get_deployment_secret("DB_HOST", raise_on_error=True)

    async def test_sole_entry_under_another_name_is_the_value(self) -> None:
        # A Kubernetes secret named DB_PASSWORD whose one data key is "password".
        client = _client(_not_found("ATLAN_DEPLOYMENT_SECRETS"), {"password": "pw"})
        with _patch_client(client):
            result = await get_deployment_secret("DB_PASSWORD", raise_on_error=True)

        assert result == "pw"


@pytest.mark.usefixtures("_deployed")
class TestGetDeploymentSecretDeprecatedDefault:
    async def test_omitting_raise_on_error_warns_and_returns_none(self) -> None:
        client = _client(_not_found("ATLAN_DEPLOYMENT_SECRETS"), _not_found("K"))
        with _patch_client(client), pytest.warns(DeprecationWarning, match="v4.0"):
            result = await get_deployment_secret("K")

        assert result is None

    async def test_explicit_false_does_not_warn(self) -> None:
        client = _client({"ATLAN_DEPLOYMENT_SECRETS": json.dumps({"K": "v"})})
        with _patch_client(client), warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await get_deployment_secret("K", raise_on_error=False)

        assert result == "v"

    async def test_legacy_mode_does_not_retry_a_cold_sidecar(self) -> None:
        client = MagicMock(
            get_secret=AsyncMock(side_effect=httpx.ConnectError("refused")),
            close=AsyncMock(),
        )
        with _patch_client(client):
            result = await get_deployment_secret("K", raise_on_error=False)

        assert result is None
        assert client.get_secret.await_count == 1


@pytest.mark.usefixtures("_deployed")
class TestGetDeploymentSecretEmptyValueIsAbsent:
    """The local.env store answers an unset variable with "" instead of an error."""

    async def test_empty_single_key_value_raises_not_found(self) -> None:
        client = _client(_not_found("ATLAN_DEPLOYMENT_SECRETS"), {"DB_PASSWORD": ""})
        with _patch_client(client), pytest.raises(SecretNotFoundError):
            await get_deployment_secret("DB_PASSWORD", raise_on_error=True)

    async def test_empty_bundle_value_falls_through_to_single_key(self) -> None:
        client = _client(
            {"ATLAN_DEPLOYMENT_SECRETS": json.dumps({"DB_PASSWORD": ""})},
            {"DB_PASSWORD": "pw"},
        )
        with _patch_client(client):
            result = await get_deployment_secret("DB_PASSWORD", raise_on_error=True)

        assert result == "pw"

    async def test_empty_sole_entry_raises_not_found(self) -> None:
        client = _client(_not_found("ATLAN_DEPLOYMENT_SECRETS"), {"password": ""})
        with _patch_client(client), pytest.raises(SecretNotFoundError):
            await get_deployment_secret("DB_PASSWORD", raise_on_error=True)

    async def test_legacy_mode_returns_none_for_empty_value(self) -> None:
        client = _client(_not_found("ATLAN_DEPLOYMENT_SECRETS"), {"DB_PASSWORD": ""})
        with _patch_client(client):
            result = await get_deployment_secret("DB_PASSWORD", raise_on_error=False)

        assert result is None


@pytest.mark.usefixtures("_deployed")
class TestGetDeploymentSecretClientConstruction:
    async def test_construction_failure_raises_non_retryable_store_error(
        self,
    ) -> None:
        failure = httpx.InvalidURL("Invalid port: 'abc'")
        with (
            patch(
                "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
                side_effect=failure,
            ),
            pytest.raises(SecretStoreError) as ei,
        ):
            await get_deployment_secret("DB_HOST", raise_on_error=True)

        assert ei.value.cause is failure
        assert ei.value.effective_retryable is False
