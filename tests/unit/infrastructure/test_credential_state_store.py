"""Unit tests for credential interception via state store and ATLAN_LOCAL_DEVELOPMENT guard.

Tests the pattern used by handler/service.py to store inline credentials
in the state store during local development, and the resolver's state store
lookup path.
"""

from __future__ import annotations

import json
import os

from application_sdk.handler.service import _normalize_credentials, _pairs_to_flat
from application_sdk.testing.mocks import MockSecretStore, MockStateStore


class TestCredentialStateStoreRoundtrip:
    """Credential interception: save via state store, resolve via resolver."""

    async def test_save_and_resolve_via_state_store(self):
        """Full roundtrip: store creds in state store, resolver reads them back."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        # Simulate handler: store creds in state store under cred: prefix
        guid = "test-guid-abc123"
        flat_creds = {"host": "db.example.com", "port": "5432"}
        await state_store.save(f"cred:{guid}", flat_creds)

        ctx = InfrastructureContext(state_store=state_store, secret_store=secret_store)
        set_infrastructure(ctx)

        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(name=guid, credential_type="unknown", credential_guid=guid)
        result = await resolver.resolve_raw(ref)

        assert result["host"] == "db.example.com"
        assert result["port"] == "5432"

        set_infrastructure(InfrastructureContext())

    async def test_state_store_takes_precedence_over_secret_store(self):
        """State store (from /start handler) takes precedence over secret store."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        guid = "dual-guid"
        await state_store.save(f"cred:{guid}", {"source": "state_store"})
        secret_store.set(guid, json.dumps({"source": "secret_store"}))

        ctx = InfrastructureContext(state_store=state_store, secret_store=secret_store)
        set_infrastructure(ctx)

        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(name=guid, credential_type="unknown", credential_guid=guid)
        result = await resolver.resolve_raw(ref)

        assert result["source"] == "state_store"

        set_infrastructure(InfrastructureContext())

    async def test_fallback_to_secret_store(self):
        """Falls back to secret store when not in state store."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        guid = "secret-only"
        secret_store.set(guid, json.dumps({"host": "from-secret"}))

        ctx = InfrastructureContext(state_store=state_store, secret_store=secret_store)
        set_infrastructure(ctx)

        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(name=guid, credential_type="unknown", credential_guid=guid)
        result = await resolver.resolve_raw(ref)

        assert result["host"] == "from-secret"

        set_infrastructure(InfrastructureContext())


class TestLocalDevGuard:
    """Test ATLAN_LOCAL_DEVELOPMENT env var guard."""

    def test_guard_allows_when_true(self, monkeypatch):
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "true")
        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local is True

    def test_guard_blocks_when_false(self, monkeypatch):
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "false")
        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local is False

    def test_guard_blocks_when_unset(self, monkeypatch):
        monkeypatch.delenv("ATLAN_LOCAL_DEVELOPMENT", raising=False)
        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local is False

    def test_guard_allows_when_1(self, monkeypatch):
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "1")
        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local is True

    async def test_full_interception_flow_with_guard(self, monkeypatch):
        """End-to-end: normalize → guard → store → verify."""
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "true")

        state_store = MockStateStore()
        body = {
            "credentials": [
                {"key": "host", "value": "db.example.com"},
                {"key": "password", "value": "s3cr3t"},
            ],
        }
        body = _normalize_credentials(body)

        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local

        flat_creds = _pairs_to_flat(body["credentials"])
        await state_store.save("cred:test-guid", flat_creds)
        body["credential_guid"] = "test-guid"
        del body["credentials"]

        assert "credentials" not in body
        assert body["credential_guid"] == "test-guid"

        result = await state_store.load("cred:test-guid")
        assert result["host"] == "db.example.com"
        assert result["password"] == "s3cr3t"


class TestMockProtocolCompliance:
    """Verify Mock classes implement Protocol correctly."""

    async def test_state_store_save_load_delete(self):
        store = MockStateStore()
        await store.save("k1", {"v": 1})
        assert await store.load("k1") == {"v": 1}
        assert await store.delete("k1") is True
        assert await store.load("k1") is None

    async def test_secret_store_set_get(self):
        store = MockSecretStore()
        store.set("secret", "value")
        assert await store.get("secret") == "value"

    async def test_state_store_call_tracking(self):
        store = MockStateStore()
        await store.save("k", {"v": 1})
        await store.load("k")
        await store.delete("k")
        assert len(store.get_save_calls()) == 1
        assert len(store.get_load_calls()) == 1
        assert len(store.get_delete_calls()) == 1

    async def test_secret_store_call_tracking(self):
        store = MockSecretStore()
        store.set("s", "v")
        await store.get("s")
        assert len(store.get_get_calls()) == 1
