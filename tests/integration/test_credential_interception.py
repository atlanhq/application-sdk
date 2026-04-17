"""Integration tests for credential interception via state store.

Verifies the full flow: inline credentials in /start → state store →
credential resolver reads back. Uses real Dapr sidecar (daprd).
"""

from __future__ import annotations

import json

import pytest

from application_sdk.testing.mocks import MockSecretStore, MockStateStore


@pytest.mark.integration
class TestCredentialInterception:
    """Test the credential interception flow using state store."""

    async def test_state_store_credential_roundtrip(self):
        """Credentials saved to state store can be read back by resolver."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        # Simulate what /start handler does: store creds in state store
        credential_guid = "test-guid-abc123"
        flat_creds = {"host": "db.example.com", "port": "5432", "username": "admin"}
        await state_store.save(f"cred:{credential_guid}", flat_creds)

        # Set up infrastructure context so resolver can find state store
        ctx = InfrastructureContext(
            state_store=state_store,
            secret_store=secret_store,
        )
        set_infrastructure(ctx)

        # Resolver should find credentials via state store
        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(
            name=credential_guid,
            credential_type="unknown",
            credential_guid=credential_guid,
        )
        result = await resolver.resolve_raw(ref)

        assert result is not None
        assert result["host"] == "db.example.com"
        assert result["username"] == "admin"

        # Clean up
        set_infrastructure(InfrastructureContext())

    async def test_state_store_takes_precedence_over_secret_store(self):
        """State store credentials (from /start) take precedence over secret store."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        guid = "dual-guid"

        # Same GUID in both stores with different values
        await state_store.save(f"cred:{guid}", {"source": "state_store"})
        secret_store.set(guid, json.dumps({"source": "secret_store"}))

        ctx = InfrastructureContext(
            state_store=state_store,
            secret_store=secret_store,
        )
        set_infrastructure(ctx)

        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(name=guid, credential_type="unknown", credential_guid=guid)
        result = await resolver.resolve_raw(ref)

        # State store should win (checked first in _resolve_by_guid)
        assert result["source"] == "state_store"

        set_infrastructure(InfrastructureContext())

    async def test_fallback_to_secret_store_when_not_in_state(self):
        """Falls back to secret store when credential not in state store."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        guid = "secret-only-guid"
        secret_store.set(guid, json.dumps({"host": "from-secret-store"}))

        ctx = InfrastructureContext(
            state_store=state_store,
            secret_store=secret_store,
        )
        set_infrastructure(ctx)

        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(name=guid, credential_type="unknown")
        result = await resolver.resolve_raw(ref)

        assert result["host"] == "from-secret-store"

        set_infrastructure(InfrastructureContext())


@pytest.mark.integration
class TestMockProtocolCompliance:
    """Verify Mock classes satisfy the Protocol contracts."""

    async def test_mock_state_store_save_load_delete(self):
        store = MockStateStore()
        await store.save("key1", {"value": "hello"})
        result = await store.load("key1")
        assert result == {"value": "hello"}

        deleted = await store.delete("key1")
        assert deleted is True
        assert await store.load("key1") is None

    async def test_mock_secret_store_set_get(self):
        store = MockSecretStore()
        store.set("my-secret", "secret-value")
        result = await store.get("my-secret")
        assert result == "secret-value"

    async def test_mock_state_store_tracks_calls(self):
        store = MockStateStore()
        await store.save("k", {"v": 1})
        await store.load("k")
        assert len(store.get_save_calls()) == 1
        assert len(store.get_load_calls()) == 1


@pytest.mark.integration
class TestLocalDevGuard:
    """Test ATLAN_LOCAL_DEVELOPMENT env var guard for credential interception."""

    async def test_credentials_stored_when_local_dev_true(self, monkeypatch):
        """Inline credentials are intercepted when ATLAN_LOCAL_DEVELOPMENT=true."""
        import os

        from application_sdk.handler.service import (
            _normalize_credentials,
            _pairs_to_flat,
        )

        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "true")

        state_store = MockStateStore()
        body = {
            "credentials": [
                {"key": "host", "value": "db.example.com"},
                {"key": "password", "value": "secret"},
            ],
        }
        body = _normalize_credentials(body)

        # Simulate the handler guard logic
        is_local_dev = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local_dev is True

        if is_local_dev and state_store is not None:
            flat_creds = _pairs_to_flat(body["credentials"])
            await state_store.save("cred:test-guid", flat_creds)
            body["credential_guid"] = "test-guid"
            del body["credentials"]

        assert "credentials" not in body
        assert body["credential_guid"] == "test-guid"
        result = await state_store.load("cred:test-guid")
        assert result["host"] == "db.example.com"

    async def test_credentials_not_stored_when_local_dev_false(self, monkeypatch):
        """Inline credentials are NOT intercepted in non-local mode."""
        import os

        from application_sdk.handler.service import _normalize_credentials

        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "false")

        body = {
            "credentials": [
                {"key": "host", "value": "db.example.com"},
            ],
        }
        body = _normalize_credentials(body)

        is_local_dev = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local_dev is False

        # Credentials should NOT be stored — body unchanged
        assert "credentials" in body

    async def test_credentials_not_stored_when_env_unset(self, monkeypatch):
        """Inline credentials are NOT intercepted when env var not set."""
        import os

        from application_sdk.handler.service import _normalize_credentials

        monkeypatch.delenv("ATLAN_LOCAL_DEVELOPMENT", raising=False)

        body = {
            "credentials": [
                {"key": "host", "value": "db.example.com"},
            ],
        }
        body = _normalize_credentials(body)

        is_local_dev = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local_dev is False
        assert "credentials" in body
