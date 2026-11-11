"""Handler service tests: credential normalisation and the local vault."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from application_sdk.handler.service import (
    _flatten_to_pairs,
    _normalize_credentials,
    create_app_handler_service,
)
from tests.unit.handler._service_support import (
    _make_client,
    _TestHandler,
)


class TestNormalizeCredentials:
    """Tests for _normalize_credentials v2→v3 compat shim."""

    def test_v3_list_passthrough(self) -> None:
        body = {"credentials": [{"key": "host", "value": "localhost"}]}
        result = _normalize_credentials(body)
        assert result["credentials"] == [{"key": "host", "value": "localhost"}]

    def test_v3_empty_list_passthrough(self) -> None:
        body = {"credentials": []}
        result = _normalize_credentials(body)
        assert result["credentials"] == []

    def test_missing_credentials_passthrough(self) -> None:
        body = {"other_key": "value"}
        result = _normalize_credentials(body)
        assert result == {"other_key": "value"}

    def test_null_credentials_passthrough(self) -> None:
        body = {"credentials": None}
        result = _normalize_credentials(body)
        assert result["credentials"] is None

    def test_v2_dict_conversion(self) -> None:
        body = {
            "credentials": {
                "host": "app.mode.com",
                "username": "user1",
                "password": "secret",
            }
        }
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "app.mode.com"
        assert keys["username"] == "user1"
        assert keys["password"] == "secret"

    def test_v2_extra_flattening(self) -> None:
        body = {
            "credentials": {
                "host": "app.mode.com",
                "extra": {"workspace": "atlan", "region": "us"},
            }
        }
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "app.mode.com"
        assert keys["extra.workspace"] == "atlan"
        assert keys["extra.region"] == "us"
        assert "extra" not in keys

    def test_none_values_excluded(self) -> None:
        body = {"credentials": {"host": "localhost", "port": None}}
        result = _normalize_credentials(body)
        keys = [c["key"] for c in result["credentials"]]
        assert "host" in keys
        assert "port" not in keys

    def test_non_string_values_serialized_as_json(self) -> None:
        body = {
            "credentials": {
                "host": "localhost",
                "port": 5432,
                "ssl": True,
                "options": {"timeout": 30},
            }
        }
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "localhost"
        assert keys["port"] == "5432"
        assert keys["ssl"] == "true"
        assert keys["options"] == '{"timeout": 30}'

    def test_non_dict_extra_ignored(self) -> None:
        body = {"credentials": {"host": "localhost", "extra": "not-a-dict"}}
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "localhost"
        assert "extra" not in keys

    def test_does_not_mutate_original_body(self) -> None:
        body = {
            "credentials": {"host": "localhost"},
            "metadata": {"key": "value"},
        }
        original_meta = body["metadata"]
        result = _normalize_credentials(body)
        assert result is not body
        assert result["metadata"] is original_meta
        assert isinstance(body["credentials"], dict)

    def test_preserves_other_body_fields(self) -> None:
        body = {
            "credentials": {"host": "localhost"},
            "connection_id": "conn-123",
            "timeout_seconds": 60,
        }
        result = _normalize_credentials(body)
        assert result["connection_id"] == "conn-123"
        assert result["timeout_seconds"] == 60

    def test_v2_auth_integration(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "credentials": {
                    "host": "app.mode.com",
                    "username": "user",
                    "password": "pass",
                    "extra": {"workspace": "ws"},
                }
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "success"

    # ── Flat top-level credential format (Heracles credential test) ──────

    def test_flat_toplevel_conversion(self) -> None:
        """Heracles sends flat top-level keys: {"host": ..., "authType": ...}."""
        body = {
            "host": "myns.servicebus.windows.net:9093",
            "port": 9093,
            "authType": "basic",
            "username": "$ConnectionString",
            "password": "Endpoint=sb://myns/;SharedAccessKeyName=key;SharedAccessKey=secret",
            "extra": {"security_protocol": "SASL_SSL"},
            "connectorConfigName": "atlan-connectors-azure-event-hub",
        }
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "myns.servicebus.windows.net:9093"
        assert keys["authType"] == "basic"
        assert keys["username"] == "$ConnectionString"
        assert "Endpoint=sb://" in keys["password"]
        assert keys["extra.security_protocol"] == "SASL_SSL"
        assert keys["port"] == "9093"

    def test_flat_toplevel_no_extra(self) -> None:
        body = {"host": "localhost", "authType": "basic", "password": "secret"}
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "localhost"
        assert keys["authType"] == "basic"

    def test_flat_toplevel_preserves_other_fields(self) -> None:
        body = {
            "host": "localhost",
            "authType": "basic",
            "connection_id": "conn-123",
            "timeout_seconds": 30,
        }
        result = _normalize_credentials(body)
        assert result["connection_id"] == "conn-123"
        assert result["timeout_seconds"] == 30
        assert isinstance(result["credentials"], list)

    def test_no_credential_keys_passthrough(self) -> None:
        """Body with no known credential keys should pass through unchanged."""
        body = {"connection_id": "conn-123", "metadata": {"key": "value"}}
        result = _normalize_credentials(body)
        assert "credentials" not in result
        assert result == body

    def test_flat_toplevel_auth_integration(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "host": "app.mode.com",
                "authType": "basic",
                "username": "user",
                "password": "pass",
                "extra": {"workspace": "ws"},
                "connectorConfigName": "test-connector",
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "success"


class TestFlattenToPairs:
    """Tests for _flatten_to_pairs (flat dict → v3 list)."""

    def test_simple_keys(self) -> None:
        result = _flatten_to_pairs({"host": "db.example.com", "username": "admin"})
        keys = {p["key"]: p["value"] for p in result}
        assert keys == {"host": "db.example.com", "username": "admin"}

    def test_extra_nested(self) -> None:
        creds = dict(host="db.example.com", extra={"role": "ADMIN", "warehouse": "WH"})
        result = _flatten_to_pairs(creds)
        keys = {p["key"]: p["value"] for p in result}
        assert keys["extra.role"] == "ADMIN"
        assert keys["extra.warehouse"] == "WH"
        assert "extra" not in keys

    def test_none_values_skipped(self) -> None:
        result = _flatten_to_pairs({"host": "db.example.com", "port": None})
        keys = [p["key"] for p in result]
        assert "port" not in keys

    def test_empty_dict(self) -> None:
        assert _flatten_to_pairs({}) == []

    def test_non_dict_extra_dropped(self) -> None:
        """Non-dict extra values are silently ignored."""
        result = _flatten_to_pairs({"host": "db.example.com", "extra": "string"})
        keys = {p["key"] for p in result}
        assert keys == {"host"}

    def test_does_not_mutate_input(self) -> None:
        """_flatten_to_pairs must not mutate its argument (shared helper)."""
        creds = {"host": "db.example.com", "extra": {"role": "ADMIN"}}
        _flatten_to_pairs(creds)
        assert creds == {"host": "db.example.com", "extra": {"role": "ADMIN"}}


# ---------------------------------------------------------------------------
# Local vault credential provisioning tests
# ---------------------------------------------------------------------------


class TestProvisionLocalVault:
    """Tests for POST /workflows/v1/dev/local-vault credential provisioning endpoint."""

    def _make_local_vault_client(
        self,
        *,
        deployment_name: str = "local",
        local_environment: str = "local",
        storage: object | None = None,
    ) -> tuple[TestClient, dict[str, object]]:
        """Create a TestClient with DEPLOYMENT_NAME and storage patched.

        Returns (client, restore_dict) so the caller can restore module state.
        """
        from application_sdk.handler import service as svc_module

        handler = _TestHandler()
        app = create_app_handler_service(
            handler, app_name="vault-test", storage=storage
        )

        original_dep = svc_module.DEPLOYMENT_NAME
        original_local = svc_module.LOCAL_ENVIRONMENT
        svc_module.DEPLOYMENT_NAME = deployment_name
        svc_module.LOCAL_ENVIRONMENT = local_environment
        client = TestClient(app, raise_server_exceptions=False)
        restore = {
            "dep": original_dep,
            "local": original_local,
            "module": svc_module,
        }
        return client, restore

    def _restore(self, restore: dict[str, object]) -> None:
        svc_module = restore["module"]
        svc_module.DEPLOYMENT_NAME = restore["dep"]  # type: ignore[attr-defined]
        svc_module.LOCAL_ENVIRONMENT = restore["local"]  # type: ignore[attr-defined]

    def test_happy_path_returns_200_with_credential_guid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST full credential body returns 200 with credential_guid, writes secrets.json."""
        # storage=None means objectstore save is a no-op (returns False)
        client, restore = self._make_local_vault_client(storage=None)

        body = {
            "username": "admin",
            "password": "secret123",
            "host": "db.example.com",
            "port": 5432,
            "extra": {"ssl": True},
            "url": "jdbc:postgresql://db.example.com:5432",
        }

        try:
            monkeypatch.chdir(tmp_path)
            response = client.post("/workflows/v1/dev/local-vault", json=body)

            assert response.status_code == 200
            resp_body = response.json()
            assert resp_body["success"] is True
            guid = resp_body["data"]["credential_guid"]
            # Verify it's a valid hex string (uuid4().hex)
            assert len(guid) == 32
            int(guid, 16)  # raises ValueError if not valid hex

            # Verify secrets.json was written with sensitive fields
            import orjson

            secrets_file = tmp_path / "local" / "dapr" / "secrets" / "secrets.json"
            assert secrets_file.exists()
            all_secrets = orjson.loads(secrets_file.read_bytes())
            assert guid in all_secrets
            stored_sensitive = all_secrets[guid]
            assert stored_sensitive["username"] == "admin"
            assert stored_sensitive["password"] == "secret123"
            assert stored_sensitive["extra"] == {"ssl": True}
            assert stored_sensitive["url"] == "jdbc:postgresql://db.example.com:5432"
            # Non-sensitive fields should NOT be in secrets
            assert "host" not in stored_sensitive
            assert "port" not in stored_sensitive
        finally:
            self._restore(restore)

    def test_dev_only_gate_rejects_non_local(self) -> None:
        """When DEPLOYMENT_NAME != LOCAL_ENVIRONMENT, returns 403."""
        client, restore = self._make_local_vault_client(
            deployment_name="production", local_environment="local"
        )
        try:
            response = client.post(
                "/workflows/v1/dev/local-vault", json={"host": "db.example.com"}
            )
            assert response.status_code == 403
            assert "Dev-only" in response.json()["detail"]
        finally:
            self._restore(restore)

    def test_sensitive_nonsensitive_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify SENSITIVE_FIELDS go to secrets.json, everything else to object storage."""
        from unittest.mock import MagicMock

        mock_storage = MagicMock()  # non-None so objectstore save runs
        client, restore = self._make_local_vault_client(storage=mock_storage)

        body = {
            "username": "user1",
            "password": "pass1",
            "extra": {"key": "val"},
            "url": "jdbc://host",
            "driverProperties": {"prop": "val"},
            "sodaConnection": "soda://conn",
            "host": "db.example.com",
            "port": 5432,
            "authType": "basic",
        }

        captured_config: dict = {}

        async def capture_put_json(key, body_arg, store):
            captured_config.update(body_arg)

        try:
            with patch(
                "application_sdk.storage.ops.put_json",
                side_effect=capture_put_json,
            ):
                monkeypatch.chdir(tmp_path)
                response = client.post("/workflows/v1/dev/local-vault", json=body)

            assert response.status_code == 200
            guid = response.json()["data"]["credential_guid"]

            # Check secrets.json for sensitive fields
            import orjson

            secrets_file = tmp_path / "local" / "dapr" / "secrets" / "secrets.json"
            all_secrets = orjson.loads(secrets_file.read_bytes())
            sensitive = all_secrets[guid]
            assert set(sensitive.keys()) == {
                "username",
                "password",
                "extra",
                "url",
                "driverProperties",
                "sodaConnection",
            }

            # Non-sensitive fields should be in the captured config
            assert "host" in captured_config
            assert "port" in captured_config
            assert "authType" in captured_config
            assert captured_config["credentialSource"] == "direct"
            # Sensitive fields should NOT be in config
            assert "username" not in captured_config
            assert "password" not in captured_config
        finally:
            self._restore(restore)

    def test_auto_generates_uuid_hex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the returned guid is a valid 32-char hex string."""
        client, restore = self._make_local_vault_client(storage=None)

        try:
            monkeypatch.chdir(tmp_path)
            response = client.post(
                "/workflows/v1/dev/local-vault", json={"host": "example.com"}
            )

            assert response.status_code == 200
            guid = response.json()["data"]["credential_guid"]
            assert isinstance(guid, str)
            assert len(guid) == 32
            # Must be valid hex
            int(guid, 16)
        finally:
            self._restore(restore)

    def test_empty_body_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty body should still work — empty secrets + empty config."""
        client, restore = self._make_local_vault_client(storage=None)

        try:
            monkeypatch.chdir(tmp_path)
            response = client.post("/workflows/v1/dev/local-vault", json={})

            assert response.status_code == 200
            guid = response.json()["data"]["credential_guid"]

            # secrets.json should exist with empty sensitive dict
            import orjson

            secrets_file = tmp_path / "local" / "dapr" / "secrets" / "secrets.json"
            all_secrets = orjson.loads(secrets_file.read_bytes())
            assert all_secrets[guid] == {}
        finally:
            self._restore(restore)

    def test_no_storage_still_writes_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When storage is None, secrets are written but objectstore save is a no-op."""
        client, restore = self._make_local_vault_client(storage=None)

        try:
            monkeypatch.chdir(tmp_path)
            response = client.post(
                "/workflows/v1/dev/local-vault",
                json={"username": "u", "host": "h"},
            )

            # The endpoint still succeeds because _config_save_to_objectstore
            # returns False (no-op) when storage is None, but doesn't raise.
            assert response.status_code == 200
            guid = response.json()["data"]["credential_guid"]

            import orjson

            secrets_file = tmp_path / "local" / "dapr" / "secrets" / "secrets.json"
            all_secrets = orjson.loads(secrets_file.read_bytes())
            assert all_secrets[guid] == {"username": "u"}
        finally:
            self._restore(restore)
