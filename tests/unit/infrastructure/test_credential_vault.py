"""Unit tests for CredentialVault and DaprCredentialVault."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from application_sdk.errors.leaves import ColdStartRaceError
from application_sdk.infrastructure._dapr.credential_vault import (
    DaprCredentialVault,
    _resolve_credentials,
)
from application_sdk.infrastructure._secret_utils import (
    process_secret_data as _process_secret_data,
)
from application_sdk.infrastructure.credential_vault import CredentialVaultError

# ---------------------------------------------------------------------------
# _process_secret_data
# ---------------------------------------------------------------------------


class TestProcessSecretData:
    def test_multi_key_dict_returned_as_is(self) -> None:
        data = {"a": "1", "b": "2"}
        assert _process_secret_data(data) == data

    def test_single_key_with_json_dict_value_is_unwrapped(self) -> None:
        nested = {"username": "u", "password": "p"}
        result = _process_secret_data({"data": json.dumps(nested)})
        assert result == nested

    def test_single_key_plain_value_returned_as_is(self) -> None:
        result = _process_secret_data({"key": "value"})
        assert result == {"key": "value"}

    def test_scalar_map_container_coerced_to_dict(self) -> None:
        """Verify collections.abc.Mapping inputs are accepted."""
        import collections

        class FakeMapping(collections.abc.Mapping):
            def __init__(self, d: dict) -> None:
                self._d = d

            def __getitem__(self, k: str) -> Any:
                return self._d[k]

            def __iter__(self):
                return iter(self._d)

            def __len__(self) -> int:
                return len(self._d)

        result = _process_secret_data(FakeMapping({"k": "v"}))
        assert result == {"k": "v"}


# ---------------------------------------------------------------------------
# _resolve_credentials
# ---------------------------------------------------------------------------


class TestResolveCredentials:
    def test_simple_substitution(self) -> None:
        config = {"host": "db.example.com", "password": "pass_key"}
        secrets = {"pass_key": "actual_pass"}
        result = _resolve_credentials(config, secrets)
        assert result["password"] == "actual_pass"
        assert result["host"] == "db.example.com"

    def test_extra_field_substitution(self) -> None:
        config = {"host": "h", "extra": {"ssl_cert": "cert_key"}}
        secrets = {"cert_key": "-----BEGIN CERT-----"}
        result = _resolve_credentials(config, secrets)
        assert result["extra"]["ssl_cert"] == "-----BEGIN CERT-----"

    def test_no_matching_key_leaves_original(self) -> None:
        config = {"password": "literal_password"}
        result = _resolve_credentials(config, {"unrelated": "val"})
        assert result["password"] == "literal_password"

    def test_returns_deep_copy(self) -> None:
        config = {"k": "ref"}
        secrets = {"ref": "resolved"}
        result = _resolve_credentials(config, secrets)
        result["k"] = "mutated"
        # Original config must be unmodified.
        assert config["k"] == "ref"


# ---------------------------------------------------------------------------
# DaprCredentialVault.get_credentials (mocked Dapr)
# ---------------------------------------------------------------------------


def _make_mock_dapr_client(
    config_bytes: bytes | None,
    secret_data: dict[str, str],
) -> MagicMock:
    """Build a mock AsyncDaprClient that returns *config_bytes* on invoke_binding
    and *secret_data* on get_secret."""
    from application_sdk.infrastructure._dapr.http import BindingResult

    mock_client = MagicMock()

    # Binding response — AsyncDaprClient returns BindingResult (pydantic model)
    mock_client.invoke_binding = AsyncMock(
        return_value=BindingResult(data=config_bytes, metadata={})
    )

    # Secret response — AsyncDaprClient returns dict directly
    mock_client.get_secret = AsyncMock(return_value=secret_data)

    return mock_client


class TestDaprCredentialVaultGetCredentials:
    """Tests for DaprCredentialVault.get_credentials with mocked Dapr."""

    def _make_vault(self, mock_client: MagicMock) -> DaprCredentialVault:
        return DaprCredentialVault(
            mock_client,
            upstream_binding_name="upstream-objectstore",
            secret_store_name="secretstore",
        )

    async def test_direct_multi_key_mode(self) -> None:
        """DIRECT source + multi-key bundle → bundle merged into config."""
        config = {"credentialSource": "direct", "host": "db.example.com"}
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={"password": "secret_pass"},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        assert result["host"] == "db.example.com"
        assert result["password"] == "secret_pass"

    async def test_missing_config_raises_credential_vault_error(self) -> None:
        """No config bytes → _fetch_credential_config raises CredentialVaultError."""
        mock_client = _make_mock_dapr_client(
            config_bytes=None,
            secret_data={},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            with pytest.raises(CredentialVaultError):
                await vault.get_credentials("ghost-guid")

    async def test_local_env_skips_secret_fetch(self) -> None:
        """In LOCAL_ENVIRONMENT, _get_secret returns {} so no Dapr secret call is made."""
        config = {"credentialSource": "direct", "user": "admin"}
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={"password": "should_not_appear"},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "local"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        # Secret should not be fetched in local env
        mock_client.get_secret.assert_not_called()
        assert result["user"] == "admin"

    async def test_guid_passed_as_string_not_dict(self) -> None:
        """Regression: get_credentials must receive the GUID string directly."""
        captured: list[str] = []
        config = {"credentialSource": "direct"}
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={},
        )
        original_fetch = DaprCredentialVault._fetch_credential_config

        async def _capturing_fetch(self_: Any, guid: str) -> dict[str, Any]:
            captured.append(guid)
            return await original_fetch(self_, guid)

        with (
            patch.object(
                DaprCredentialVault, "_fetch_credential_config", _capturing_fetch
            ),
            patch("application_sdk.constants.DEPLOYMENT_NAME", "local"),
        ):
            vault = self._make_vault(mock_client)
            await vault.get_credentials("abc-123")

        assert captured == ["abc-123"], f"Expected string guid, got: {captured}"

    async def test_vault_error_on_binding_failure(self) -> None:
        """Dapr binding error → wrapped in CredentialVaultError."""
        mock_client = MagicMock()
        mock_client.invoke_binding = AsyncMock(
            side_effect=RuntimeError("Dapr unavailable")
        )

        vault = self._make_vault(mock_client)
        with pytest.raises(CredentialVaultError):
            await vault.get_credentials("bad-guid")

    async def test_agent_multi_key_mode_with_secret_path(self) -> None:
        """AGENT source + secret-path → fetches multi-key bundle via secret-path key."""
        config = {
            "credentialSource": "agent",
            "secret-path": "apps/myapp/creds/my-secret",
            "host": "host_ref",
        }
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={"host_ref": "db.example.com"},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        # AGENT mode: _resolve_credentials substitutes host_ref → db.example.com
        assert result["host"] == "db.example.com"
        # Secret was fetched with the secret-path key, not the GUID
        mock_client.get_secret.assert_called_once_with(
            store_name="secretstore",
            key="apps/myapp/creds/my-secret",
        )

    async def test_agent_single_key_mode(self) -> None:
        """AGENT source without secret-path → single-key mode (one lookup per field).

        In single-key mode each config field value is used as a secret key.
        _fetch_single_key_secrets collects results keyed by the secret's own key
        names so that _resolve_credentials can substitute config field values that
        equal a collected key.  The secret store must therefore return the resolved
        value under the same key that appears as the config field's value.
        """
        config = {
            "credentialSource": "agent",
            "password": "secret_key_ref",
        }
        # Secret is stored as {"secret_key_ref": "actual_password"} so that
        # _resolve_credentials can match config["password"] == "secret_key_ref".
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={"secret_key_ref": "actual_password"},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        assert result.get("password") == "actual_password"

    async def test_agent_single_key_mode_outage_raises_not_silently_swallowed(
        self, deterministic_dapr_cold_start_deadline
    ) -> None:
        """Single-key mode must not silently proceed with an incomplete
        credential when the secret store genuinely never answers — that's
        the same swallow already fixed for multi-key mode (see
        test_persistent_transient_failure_raises_not_silently_swallowed);
        single-key probing must still fail loudly on an exhausted
        cold-start outage rather than treating it like a non-secret field."""
        config = {
            "credentialSource": "agent",
            "password": "secret_key_ref",
        }
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(), secret_data={}
        )
        mock_client.get_secret = AsyncMock(
            side_effect=httpx.ConnectError("all connection attempts failed")
        )

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            with pytest.raises(CredentialVaultError):
                await vault.get_credentials("my-guid")

    async def test_guid_validation_rejects_path_traversal(self) -> None:
        """GUIDs with unsafe characters (e.g. '../') raise CredentialVaultError immediately."""
        mock_client = _make_mock_dapr_client(config_bytes=b"", secret_data={})
        vault = self._make_vault(mock_client)

        with pytest.raises(CredentialVaultError, match="Invalid credential GUID"):
            await vault.get_credentials("../../etc/passwd")

    async def test_guid_validation_rejects_slash(self) -> None:
        """Slashes in GUIDs are rejected before any network call."""
        mock_client = _make_mock_dapr_client(config_bytes=b"", secret_data={})
        vault = self._make_vault(mock_client)

        with pytest.raises(CredentialVaultError, match="Invalid credential GUID"):
            await vault.get_credentials("valid-prefix/injected")

        mock_client.invoke_binding.assert_not_called()


# ---------------------------------------------------------------------------
# DaprCredentialVault._get_secret cold-start retry / classification
# ---------------------------------------------------------------------------


class TestDaprCredentialVaultColdStartRetry:
    """The GUID/vault path races the same cold Dapr sidecar the bundle/named
    paths do, and must retry a transient failure rather than silently
    treating it as an absent bundle (a cold-start race here used to look
    identical to "no secret", degrading to an incomplete credential)."""

    def _make_vault(self, mock_client: MagicMock) -> DaprCredentialVault:
        return DaprCredentialVault(
            mock_client,
            upstream_binding_name="upstream-objectstore",
            secret_store_name="secretstore",
        )

    async def test_transient_failure_is_retried_then_succeeds(
        self, fast_dapr_cold_start_retry
    ) -> None:
        calls = {"n": 0}

        async def _cold_then_ready(*, store_name: str, key: str) -> dict[str, str]:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("all connection attempts failed")
            return {"password": "secret_pass"}

        mock_client = _make_mock_dapr_client(config_bytes=b"{}", secret_data={})
        mock_client.get_secret = AsyncMock(side_effect=_cold_then_ready)

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault._get_secret("my-guid")

        assert result == {"password": "secret_pass"}
        assert calls["n"] == 3

    async def test_secretstore_cold_start_not_masked_by_binding_success(
        self, fast_dapr_cold_start_retry
    ) -> None:
        """The upstream object-store binding and the secret store are
        distinct Dapr components racing the same cold sidecar independently
        — the binding's config-fetch answering immediately (e.g. it happened
        to finish initializing first) must not arm the secret store's gate
        too. Exercises the full get_credentials() flow, where both steps
        run in the same call."""
        config = {"credentialSource": "direct", "host": "db.example.com"}
        calls = {"n": 0}

        async def _cold_then_ready(*, store_name: str, key: str) -> dict[str, str]:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("all connection attempts failed")
            return {"password": "secret_pass"}

        # Config-fetch (binding) succeeds immediately — this alone must not
        # arm the secret store's cold-start gate.
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(), secret_data={}
        )
        mock_client.get_secret = AsyncMock(side_effect=_cold_then_ready)

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        assert result["password"] == "secret_pass"
        assert calls["n"] == 3

    async def test_persistent_transient_failure_raises_not_silently_swallowed(
        self, deterministic_dapr_cold_start_deadline
    ) -> None:
        config = {"credentialSource": "direct", "host": "db.example.com"}
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(), secret_data={}
        )
        mock_client.get_secret = AsyncMock(
            side_effect=httpx.ConnectError("all connection attempts failed")
        )

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            # Prior behavior: this silently proceeded with secret_data={}.
            # A persistent cold-start-shaped failure must surface as a real
            # error instead of degrading to an incomplete credential.
            with pytest.raises(CredentialVaultError):
                await vault.get_credentials("my-guid")

    async def test_4xx_rejection_is_not_retried(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
            30.0,
        )
        config = {"credentialSource": "direct", "host": "db.example.com"}
        req = httpx.Request("GET", "http://localhost/secret")
        resp = httpx.Response(403, request=req)
        calls = {"n": 0}

        async def _forbidden(*, store_name: str, key: str) -> dict[str, str]:
            calls["n"] += 1
            raise httpx.HTTPStatusError("forbidden", request=req, response=resp)

        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(), secret_data={}
        )
        mock_client.get_secret = AsyncMock(side_effect=_forbidden)

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            with pytest.raises(CredentialVaultError):
                await vault.get_credentials("my-guid")

        # A definitive rejection fails fast — never retried.
        assert calls["n"] == 1

    async def test_definitively_absent_bundle_proceeds_with_empty_secrets(
        self,
    ) -> None:
        """An empty-but-200 response is a genuine "no secret" — not an error."""
        config = {"credentialSource": "direct", "host": "db.example.com"}
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(), secret_data={}
        )

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        assert result["host"] == "db.example.com"
        assert "password" not in result


# ---------------------------------------------------------------------------
# DaprCredentialVault._fetch_credential_config cold-start retry / classification
# ---------------------------------------------------------------------------


class TestFetchCredentialConfigColdStartRetry:
    """The config-fetch step is typically the *first* Dapr call
    get_credentials() makes, racing the identical cold sidecar the
    secret-fetch step does. A transient failure here must be retried and
    tagged ColdStartRaceError, not collapsed into the same BindingError the
    (still-current) "no config found" case produces — see the sibling
    tests in TestDaprCredentialVaultGetCredentials for that latter case."""

    def _make_vault(self, mock_client: MagicMock) -> DaprCredentialVault:
        return DaprCredentialVault(
            mock_client,
            upstream_binding_name="upstream-objectstore",
            secret_store_name="secretstore",
        )

    async def test_transient_failure_is_retried_then_succeeds(
        self, fast_dapr_cold_start_retry
    ) -> None:
        from application_sdk.infrastructure._dapr.http import BindingResult

        config = {"credentialSource": "direct", "host": "db.example.com"}
        calls = {"n": 0}

        async def _cold_then_ready(
            *, binding_name: str, operation: str, data: bytes, metadata: dict[str, str]
        ) -> BindingResult:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("all connection attempts failed")
            return BindingResult(data=json.dumps(config).encode(), metadata={})

        mock_client = _make_mock_dapr_client(config_bytes=b"{}", secret_data={})
        mock_client.invoke_binding = AsyncMock(side_effect=_cold_then_ready)

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        assert result["host"] == "db.example.com"
        assert calls["n"] == 3

    async def test_persistent_transient_failure_tags_cold_start_cause(
        self, deterministic_dapr_cold_start_deadline
    ) -> None:
        """A cold-start race here must NOT be indistinguishable from a
        genuinely-missing config — the resolver keys off `.cause` being a
        ColdStartRaceError to avoid misreporting a retryable platform outage
        as a non-retryable "credential not found"."""
        mock_client = _make_mock_dapr_client(config_bytes=b"{}", secret_data={})
        mock_client.invoke_binding = AsyncMock(
            side_effect=httpx.ConnectError("all connection attempts failed")
        )

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            with pytest.raises(CredentialVaultError) as exc_info:
                await vault.get_credentials("my-guid")

        assert isinstance(exc_info.value.cause, ColdStartRaceError)

    async def test_5xx_transient_failure_tags_cold_start_cause(
        self, deterministic_dapr_cold_start_deadline
    ) -> None:
        """A bare 5xx on the *binding* fetch is unambiguously cold-start-shaped
        (unlike the secrets API, a missing config blob is a successful
        response with empty data, not a 5xx — see is_dapr_transport_unavailable's
        docstring) — it must be retried and tagged ColdStartRaceError via
        is_dapr_transport_unavailable, exactly like a bare ConnectError."""
        req = httpx.Request("POST", "http://localhost/bindings/upstream-objectstore")
        resp = httpx.Response(503, request=req)
        mock_client = _make_mock_dapr_client(config_bytes=b"{}", secret_data={})
        mock_client.invoke_binding = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "service unavailable", request=req, response=resp
            )
        )

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            with pytest.raises(CredentialVaultError) as exc_info:
                await vault.get_credentials("my-guid")

        assert isinstance(exc_info.value.cause, ColdStartRaceError)

    async def test_4xx_rejection_is_not_retried(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
            30.0,
        )
        req = httpx.Request("POST", "http://localhost/bindings/upstream-objectstore")
        resp = httpx.Response(403, request=req)
        calls = {"n": 0}

        async def _forbidden(**kwargs: Any) -> Any:
            calls["n"] += 1
            raise httpx.HTTPStatusError("forbidden", request=req, response=resp)

        mock_client = _make_mock_dapr_client(config_bytes=b"{}", secret_data={})
        mock_client.invoke_binding = AsyncMock(side_effect=_forbidden)

        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            with pytest.raises(CredentialVaultError) as exc_info:
                await vault.get_credentials("my-guid")

        # A definitive rejection fails fast — never retried — and is not
        # tagged as a cold-start race.
        assert calls["n"] == 1
        assert not isinstance(exc_info.value.cause, ColdStartRaceError)


# ---------------------------------------------------------------------------
# _get_local_secret tests
# ---------------------------------------------------------------------------


class TestGetLocalSecret:
    """Tests for DaprCredentialVault._get_local_secret."""

    def _make_vault(self) -> DaprCredentialVault:
        mock_client = MagicMock()
        return DaprCredentialVault(
            mock_client,
            upstream_binding_name="upstream-objectstore",
            secret_store_name="secretstore",
        )

    def test_happy_path_returns_secret_dict(self, tmp_path):
        """Create secrets.json with a key, verify _get_local_secret returns it."""
        import os

        secrets_dir = tmp_path / "local" / "dapr" / "secrets"
        secrets_dir.mkdir(parents=True)
        secrets_file = secrets_dir / "secrets.json"
        secrets_data = {
            "my-guid": {"username": "admin", "password": "secret"},
            "other-guid": {"api_key": "xyz"},
        }
        secrets_file.write_text(json.dumps(secrets_data))

        vault = self._make_vault()
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = vault._get_local_secret("my-guid")
        finally:
            os.chdir(original_cwd)

        assert result == {"username": "admin", "password": "secret"}

    def test_missing_key_returns_empty_dict(self, tmp_path):
        """Key not in secrets.json returns {}."""
        import os

        secrets_dir = tmp_path / "local" / "dapr" / "secrets"
        secrets_dir.mkdir(parents=True)
        secrets_file = secrets_dir / "secrets.json"
        secrets_file.write_text(json.dumps({"other-guid": {"key": "val"}}))

        vault = self._make_vault()
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = vault._get_local_secret("nonexistent-guid")
        finally:
            os.chdir(original_cwd)

        assert result == {}

    def test_missing_file_returns_empty_dict(self, tmp_path):
        """secrets.json doesn't exist returns {}."""
        import os

        vault = self._make_vault()
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = vault._get_local_secret("any-guid")
        finally:
            os.chdir(original_cwd)

        assert result == {}

    def test_malformed_json_returns_empty_dict(self, tmp_path):
        """secrets.json has invalid JSON returns {}."""
        import os

        secrets_dir = tmp_path / "local" / "dapr" / "secrets"
        secrets_dir.mkdir(parents=True)
        secrets_file = secrets_dir / "secrets.json"
        secrets_file.write_text("NOT VALID JSON {{{")

        vault = self._make_vault()
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = vault._get_local_secret("my-guid")
        finally:
            os.chdir(original_cwd)

        assert result == {}

    async def test_get_secret_uses_local_secret_in_local_env(self, tmp_path):
        """When DEPLOYMENT_NAME == LOCAL_ENVIRONMENT, _get_secret calls _get_local_secret."""
        import os

        secrets_dir = tmp_path / "local" / "dapr" / "secrets"
        secrets_dir.mkdir(parents=True)
        secrets_file = secrets_dir / "secrets.json"
        secrets_file.write_text(
            json.dumps(
                {"my-guid": {"username": "local-user", "password": "local-pass"}}
            )
        )

        vault = self._make_vault()
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("application_sdk.constants.DEPLOYMENT_NAME", "local"):
                result = await vault._get_secret("my-guid")
        finally:
            os.chdir(original_cwd)

        assert result == {"username": "local-user", "password": "local-pass"}
