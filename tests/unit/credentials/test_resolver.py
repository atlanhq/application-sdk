"""Unit tests for CredentialResolver."""

import json
from unittest.mock import AsyncMock

import pytest

from application_sdk.credentials.errors import (
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.credentials.ref import (
    CredentialRef,
    api_key_ref,
    basic_ref,
    legacy_credential_ref,
)
from application_sdk.credentials.resolver import CredentialResolver
from application_sdk.credentials.types import (
    ApiKeyCredential,
    BasicCredential,
    RawCredential,
)
from application_sdk.infrastructure.secrets import SecretStoreUnavailableError
from application_sdk.testing.mocks import MockSecretStore


@pytest.fixture
def store():
    return MockSecretStore()


@pytest.fixture
def resolver(store):
    return CredentialResolver(store)


class TestNewPath:
    """Tests for the new (non-legacy) resolution path."""

    async def test_resolve_api_key(self, store, resolver):
        store.set("prod-key", json.dumps({"type": "api_key", "api_key": "secret123"}))
        ref = api_key_ref("prod-key")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, ApiKeyCredential)
        assert cred.api_key == "secret123"

    async def test_resolve_basic(self, store, resolver):
        store.set(
            "db-creds", json.dumps({"type": "basic", "username": "u", "password": "p"})
        )
        ref = basic_ref("db-creds")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, BasicCredential)
        assert cred.username == "u"

    async def test_type_from_data_overrides_ref_type(self, store, resolver):
        """'type' field in JSON takes precedence over ref.credential_type."""
        store.set("cred", json.dumps({"type": "api_key", "api_key": "k"}))
        # Ref says basic, but data says api_key
        ref = CredentialRef(name="cred", credential_type="basic")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, ApiKeyCredential)

    async def test_not_found_raises(self, resolver):
        ref = api_key_ref("nonexistent")
        with pytest.raises(CredentialNotFoundError):
            await resolver.resolve(ref)

    async def test_invalid_json_raises(self, store, resolver):
        store.set("bad", "not-json")
        ref = api_key_ref("bad")
        with pytest.raises(CredentialParseError):
            await resolver.resolve(ref)

    async def test_unknown_type_raises(self, store, resolver):
        store.set("cred", json.dumps({"type": "unknown_type_xyz"}))
        ref = CredentialRef(name="cred", credential_type="unknown_type_xyz")
        with pytest.raises(CredentialParseError, match="No parser registered"):
            await resolver.resolve(ref)

    async def test_resolve_raw_returns_dict(self, store, resolver):
        data = {"type": "api_key", "api_key": "secret"}
        store.set("my-key", json.dumps(data))
        ref = api_key_ref("my-key")
        raw = await resolver.resolve_raw(ref)
        assert isinstance(raw, dict)
        assert raw["api_key"] == "secret"


class TestNamedPathColdStartRetry:
    """The named-path fetch (``_fetch_raw_json``) races the same cold Dapr
    sidecar the agent bundle fetch does, and shares its retry engine."""

    async def test_retries_transient_failure_then_succeeds(
        self, fast_dapr_cold_start_retry
    ) -> None:
        calls = {"n": 0}

        class ColdThenReady:
            async def get(self, name: str) -> str:
                calls["n"] += 1
                if calls["n"] < 3:  # sidecar still cold on the first two tries
                    raise SecretStoreUnavailableError(name)
                return json.dumps({"type": "api_key", "api_key": "secret123"})

        resolver = CredentialResolver(ColdThenReady())  # type: ignore[arg-type]

        cred = await resolver.resolve(api_key_ref("prod-key"))

        assert isinstance(cred, ApiKeyCredential)
        assert cred.api_key == "secret123"
        assert calls["n"] == 3  # rode out the two cold failures

    async def test_persistent_transient_failure_wraps_in_credential_error(
        self, deterministic_dapr_cold_start_deadline
    ) -> None:
        from application_sdk.credentials.errors import CredentialError

        # Also asserts the call count, so this test can't pass merely
        # because _fetch_raw_json wraps any exception into CredentialError —
        # it must actually have retried first.
        calls = {"n": 0}

        class AlwaysDown:
            async def get(self, name: str) -> str:
                calls["n"] += 1
                raise SecretStoreUnavailableError(name)

        resolver = CredentialResolver(AlwaysDown())  # type: ignore[arg-type]

        with pytest.raises(CredentialError):
            await resolver.resolve(api_key_ref("prod-key"))
        assert calls["n"] == 2


def _make_vault_patches(vault_return=None, vault_side_effect=None):
    """Build mock DaprCredentialVault + DaprClient patches for resolver tests.

    DaprCredentialVault and DaprClient are lazy-imported inside _resolve_by_guid,
    so they must be patched at their source modules, not on the resolver module.
    """
    from unittest.mock import MagicMock, patch

    mock_vault = MagicMock()
    if vault_side_effect is not None:
        mock_vault.get_credentials = AsyncMock(side_effect=vault_side_effect)
    else:
        mock_vault.get_credentials = AsyncMock(return_value=vault_return or {})

    p_vault = patch(
        "application_sdk.infrastructure.DaprCredentialVault",
        MagicMock(return_value=mock_vault),
    )
    mock_dapr_instance = MagicMock()
    mock_dapr_instance.close = AsyncMock()
    p_dapr = patch(
        "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
        MagicMock(return_value=mock_dapr_instance),
    )
    return p_vault, p_dapr, mock_vault


class TestGuidResolutionPath:
    """Tests for the GUID-based resolution path.

    The resolver always goes to DaprCredentialVault.get_credentials() for
    GUID-based credential resolution.
    """

    async def test_guid_resolution_uses_vault(self, store, resolver):
        """GUID resolution always goes to DaprCredentialVault."""
        p_vault, p_dapr, mock_vault = _make_vault_patches(
            vault_return={"host": "vault.example.com", "port": 5432}
        )
        with p_vault, p_dapr:
            ref = legacy_credential_ref("guid-abc")
            raw = await resolver.resolve_raw(ref)

        assert raw["host"] == "vault.example.com"
        assert raw["port"] == 5432
        mock_vault.get_credentials.assert_called_once_with("guid-abc")

    async def test_get_credentials_receives_string_not_dict(self, store, resolver):
        """Regression: resolver must pass the GUID as a plain string, not a dict."""
        from unittest.mock import MagicMock, patch

        captured: list = []
        expected_creds = {"host": "db.example.com", "port": 1025}

        async def _capture(guid):
            captured.append(guid)
            return expected_creds

        mock_vault = MagicMock()
        mock_vault.get_credentials = _capture
        mock_dapr = MagicMock()
        mock_dapr.close = AsyncMock()

        with (
            patch(
                "application_sdk.infrastructure.DaprCredentialVault",
                MagicMock(return_value=mock_vault),
            ),
            patch(
                "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
                MagicMock(return_value=mock_dapr),
            ),
        ):
            ref = legacy_credential_ref("abc-123")
            raw = await resolver.resolve_raw(ref)

        assert captured == ["abc-123"], f"Expected string guid, got: {captured}"
        assert raw["host"] == "db.example.com"

    async def test_resolve_returns_typed_credential_for_known_type(
        self, store, resolver
    ):
        """Vault returns dict → resolver parses into typed credential."""
        p_vault, p_dapr, _ = _make_vault_patches(
            vault_return={"api_key": "secret-from-vault"}
        )
        with p_vault, p_dapr:
            ref = legacy_credential_ref("abc-123", credential_type="api_key")
            cred = await resolver.resolve(ref)

        assert isinstance(cred, ApiKeyCredential)
        assert cred.api_key == "secret-from-vault"

    async def test_resolve_unknown_type_returns_raw_credential(self, store, resolver):
        """Vault returns dict, unknown type → resolver wraps in RawCredential."""
        p_vault, p_dapr, _ = _make_vault_patches(
            vault_return={"host": "db.example.com", "username": "u"}
        )
        with p_vault, p_dapr:
            ref = legacy_credential_ref("abc-123")
            cred = await resolver.resolve(ref)

        assert isinstance(cred, RawCredential)
        assert cred.get("host") == "db.example.com"

    @pytest.mark.parametrize("method", ["resolve_raw", "resolve"])
    async def test_vault_error_raises_credential_not_found(
        self, store, resolver, method
    ):
        """When the GUID is absent from the local store AND DaprCredentialVault
        raises, the resolver re-raises as CredentialNotFoundError.

        Tests both resolve_raw() and resolve() paths.
        """
        p_vault, p_dapr, _ = _make_vault_patches(
            vault_side_effect=RuntimeError("Dapr state store unavailable")
        )
        with p_vault, p_dapr:
            ref = legacy_credential_ref("abc-123")
            with pytest.raises(CredentialNotFoundError):
                await getattr(resolver, method)(ref)

    @pytest.mark.parametrize("method", ["resolve_raw", "resolve"])
    async def test_vault_dependency_unavailable_is_not_collapsed_to_not_found(
        self, store, resolver, method
    ):
        """A genuine dependency-unavailable failure from the vault (e.g. a
        cold-start retry that exhausted its budget) must propagate as-is,
        not collapse into CredentialNotFoundError — that would misreport a
        retryable platform outage as a non-retryable, user-facing
        "credential not found".

        Mirrors how DaprCredentialVault.get_credentials() actually produces
        this: its catch-all wraps the surviving ColdStartRaceError (e.g.
        SecretStoreUnavailableError, from an exhausted retry_past_dapr_cold_start
        budget) as `cause` on a generic CredentialVaultError — the resolver
        must key off that `cause`, not just the CredentialVaultError type
        itself (see the sibling "genuinely missing config" test below).
        """
        from application_sdk.infrastructure.credential_vault import CredentialVaultError
        from application_sdk.infrastructure.secrets import SecretStoreUnavailableError

        p_vault, p_dapr, _ = _make_vault_patches(
            vault_side_effect=CredentialVaultError(
                "Failed to resolve credentials for abc-123: secret store unavailable",
                credential_guid="abc-123",
                cause=SecretStoreUnavailableError("abc-123"),
            )
        )
        with p_vault, p_dapr:
            ref = legacy_credential_ref("abc-123")
            with pytest.raises(CredentialVaultError):
                await getattr(resolver, method)(ref)

    @pytest.mark.parametrize("method", ["resolve_raw", "resolve"])
    async def test_vault_genuinely_missing_config_collapses_to_not_found(
        self, store, resolver, method
    ):
        """A CredentialVaultError raised directly for a genuinely-absent
        credential config (no `cause` — e.g. DaprCredentialVault's
        "No credential config found for GUID ..." case) must still collapse
        to CredentialNotFoundError, exactly like any other definitive
        failure from the vault. This must NOT be swept up by the
        DependencyUnavailableError guard just because the deprecated
        CredentialVaultError umbrella type happens to subclass it.
        """
        from application_sdk.infrastructure.credential_vault import CredentialVaultError

        p_vault, p_dapr, _ = _make_vault_patches(
            vault_side_effect=CredentialVaultError(
                "No credential config found for GUID abc-123 in upstream store",
                credential_guid="abc-123",
            )
        )
        with p_vault, p_dapr:
            ref = legacy_credential_ref("abc-123")
            with pytest.raises(CredentialNotFoundError):
                await getattr(resolver, method)(ref)


# ---------------------------------------------------------------------------
# End-to-end credential resolution test
# ---------------------------------------------------------------------------


class TestEndToEndCredentialResolution:
    """Integration test: write secrets locally + mock object store, verify merged result."""

    async def test_resolve_raw_merges_sensitive_and_nonsensitive(self, tmp_path):
        """Full flow: secrets.json (sensitive) + mock vault config (non-sensitive)
        → resolve_raw returns merged dict with all fields.
        """
        import os
        from unittest.mock import MagicMock, patch

        guid = "e2e-test-guid-abc123"

        # 1. Write sensitive secrets to local secrets.json
        secrets_dir = tmp_path / "local" / "dapr" / "secrets"
        secrets_dir.mkdir(parents=True)
        secrets_file = secrets_dir / "secrets.json"
        sensitive_data = {
            guid: {
                "username": "admin",
                "password": "s3cret",
                "extra": {"ssl_cert": "-----BEGIN CERT-----"},
            }
        }
        secrets_file.write_text(json.dumps(sensitive_data))

        # 2. Set up mock DaprCredentialVault that reads local secrets
        #    and returns config merged with secrets (simulating local env)
        non_sensitive_config = {
            "credentialSource": "direct",
            "host": "db.example.com",
            "port": 5432,
            "authType": "basic",
        }

        from application_sdk.infrastructure._dapr.http import BindingResult

        mock_client = MagicMock()
        mock_client.invoke_binding = AsyncMock(
            return_value=BindingResult(
                data=json.dumps(non_sensitive_config).encode(), metadata={}
            )
        )
        mock_client.close = AsyncMock()

        # In local env, _get_secret reads from local file instead of Dapr
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with (
                patch("application_sdk.constants.DEPLOYMENT_NAME", "local"),
                patch(
                    "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
                    MagicMock(return_value=mock_client),
                ),
            ):
                from application_sdk.infrastructure._dapr.credential_vault import (
                    DaprCredentialVault,
                )

                vault = DaprCredentialVault(
                    mock_client,
                    upstream_binding_name="upstream",
                    secret_store_name="secretstore",
                )
                result = await vault.get_credentials(guid)
        finally:
            os.chdir(original_cwd)

        # 3. Verify merged result has both sensitive and non-sensitive fields
        assert result["host"] == "db.example.com"
        assert result["port"] == 5432
        assert result["authType"] == "basic"
        assert result["username"] == "admin"
        assert result["password"] == "s3cret"
        assert result["extra"] == {"ssl_cert": "-----BEGIN CERT-----"}
        assert result["credentialSource"] == "direct"
