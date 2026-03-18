"""Tests for credential loading logic in BaseSQLMetadataExtractionActivities._set_state.

Covers the AE/native path (inline credential + Vault merge) vs Argo path (full Vault fetch),
and the sensitive-field stripping in production.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.activities.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
)


@pytest.fixture
def activities():
    return BaseSQLMetadataExtractionActivities()


def _patch_workflow_id(wf_id="wf-1"):
    return patch(
        "application_sdk.activities.metadata_extraction.sql.get_workflow_id",
        return_value=wf_id,
    )


def _patch_secret_store_get_secret(return_value=None):
    if return_value is None:
        return_value = {}
    return patch(
        "application_sdk.activities.metadata_extraction.sql.SecretStore.get_secret",
        return_value=return_value,
    )


def _patch_secret_store_get_credentials(return_value=None):
    if return_value is None:
        return_value = {"username": "vault_user", "password": "vault_pass"}
    return patch(
        "application_sdk.activities.metadata_extraction.sql.SecretStore.get_credentials",
        new_callable=AsyncMock,
        return_value=return_value,
    )


class TestAECredentialPath:
    """Tests for the AE/native path: credential_guid + credential present."""

    @_patch_workflow_id()
    @_patch_secret_store_get_secret(return_value={"password": "from_vault"})
    @patch(
        "application_sdk.activities.metadata_extraction.sql.DEPLOYMENT_NAME",
        "local",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.sql.LOCAL_ENVIRONMENT",
        "local",
    )
    async def test_local_env_keeps_all_fields(
        self, mock_get_secret, mock_wf_id, activities
    ):
        """In local environment, sensitive fields are NOT stripped."""
        workflow_args = {
            "credential_guid": "cred-123",
            "credential": {
                "host": "localhost",
                "username": "local_user",
                "password": "local_pass",
            },
        }

        mock_sql_client = AsyncMock()
        activities.sql_client_class = lambda: mock_sql_client

        await activities._set_state(workflow_args)

        mock_sql_client.load.assert_called_once()
        loaded_config = mock_sql_client.load.call_args[0][0]
        # In local env, username/password are kept (not stripped)
        assert loaded_config["username"] == "local_user"
        # password gets overwritten by vault merge
        assert loaded_config["password"] == "from_vault"
        assert loaded_config["host"] == "localhost"

    @_patch_workflow_id()
    @_patch_secret_store_get_secret(return_value={"password": "vault_secret"})
    @patch(
        "application_sdk.activities.metadata_extraction.sql.DEPLOYMENT_NAME",
        "production-tenant",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.sql.LOCAL_ENVIRONMENT",
        "local",
    )
    async def test_production_strips_sensitive_fields(
        self, mock_get_secret, mock_wf_id, activities
    ):
        """In production, password/username are stripped before merging vault secrets."""
        credential = {
            "host": "db.prod.example.com",
            "username": "should_be_stripped",
            "password": "should_be_stripped",
            "port": 5432,
        }
        workflow_args = {
            "credential_guid": "cred-456",
            "credential": credential.copy(),
        }

        # We need to mock the sql_client_class to capture what's passed to load()
        mock_sql_client = AsyncMock()
        activities.sql_client_class = lambda: mock_sql_client

        await activities._set_state(workflow_args)

        # Verify load was called
        mock_sql_client.load.assert_called_once()
        loaded_config = mock_sql_client.load.call_args[0][0]

        # username and password should have been stripped and replaced by vault
        assert loaded_config.get("password") == "vault_secret"
        assert "username" not in loaded_config or loaded_config.get("username") is None
        # Non-sensitive fields preserved
        assert loaded_config["host"] == "db.prod.example.com"
        assert loaded_config["port"] == 5432

    @_patch_workflow_id()
    @_patch_secret_store_get_secret(return_value={})
    @patch(
        "application_sdk.activities.metadata_extraction.sql.DEPLOYMENT_NAME",
        "production",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.sql.LOCAL_ENVIRONMENT",
        "local",
    )
    async def test_production_strips_sensitive_extra_keys(
        self, mock_get_secret, mock_wf_id, activities
    ):
        """In production, sensitive keys in 'extra' dict are stripped."""
        credential = {
            "host": "db.example.com",
            "extra": {
                "client_secret": "sensitive",
                "private_key_content": "sensitive",
                "passphrase_data": "sensitive",
                "password_alt": "sensitive",
                "safe_option": "keep_me",
            },
        }
        workflow_args = {
            "credential_guid": "cred-789",
            "credential": credential.copy(),
        }

        mock_sql_client = AsyncMock()
        activities.sql_client_class = lambda: mock_sql_client

        await activities._set_state(workflow_args)

        mock_sql_client.load.assert_called_once()
        loaded_config = mock_sql_client.load.call_args[0][0]
        extra = loaded_config.get("extra", {})

        # Sensitive keys should be stripped
        assert "client_secret" not in extra
        assert "private_key_content" not in extra
        assert "passphrase_data" not in extra
        assert "password_alt" not in extra
        # Non-sensitive keys preserved
        assert extra["safe_option"] == "keep_me"

    @_patch_workflow_id()
    @_patch_secret_store_get_secret(return_value={"token": "from_vault"})
    @patch(
        "application_sdk.activities.metadata_extraction.sql.DEPLOYMENT_NAME",
        "local",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.sql.LOCAL_ENVIRONMENT",
        "local",
    )
    async def test_credential_as_json_string(
        self, mock_get_secret, mock_wf_id, activities
    ):
        """Credential provided as JSON string is parsed."""
        workflow_args = {
            "credential_guid": "cred-str",
            "credential": json.dumps({"host": "db.local", "port": 3306}),
        }

        mock_sql_client = AsyncMock()
        activities.sql_client_class = lambda: mock_sql_client

        await activities._set_state(workflow_args)

        mock_sql_client.load.assert_called_once()
        loaded_config = mock_sql_client.load.call_args[0][0]
        assert loaded_config["host"] == "db.local"
        assert loaded_config["port"] == 3306
        assert loaded_config["token"] == "from_vault"

    @_patch_workflow_id()
    @_patch_secret_store_get_secret(return_value={})
    @patch(
        "application_sdk.activities.metadata_extraction.sql.DEPLOYMENT_NAME",
        "local",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.sql.LOCAL_ENVIRONMENT",
        "local",
    )
    async def test_invalid_credential_json_string_becomes_empty(
        self, mock_get_secret, mock_wf_id, activities
    ):
        """Invalid JSON string credential falls back to empty dict."""
        workflow_args = {
            "credential_guid": "cred-bad",
            "credential": "not-valid-json",
        }

        mock_sql_client = AsyncMock()
        activities.sql_client_class = lambda: mock_sql_client

        await activities._set_state(workflow_args)

        mock_sql_client.load.assert_called_once()
        loaded_config = mock_sql_client.load.call_args[0][0]
        # Should be empty dict (parsed from invalid JSON) merged with empty vault
        assert isinstance(loaded_config, dict)


class TestArgoCredentialPath:
    """Tests for the Argo/state-store path: credential_guid without credential."""

    @_patch_workflow_id()
    @_patch_secret_store_get_credentials(
        return_value={"username": "argo_user", "password": "argo_pass"}
    )
    async def test_argo_path_uses_full_credential_fetch(
        self, mock_get_credentials, mock_wf_id, activities
    ):
        """Without 'credential' key, falls back to full SecretStore.get_credentials."""
        workflow_args = {
            "credential_guid": "cred-argo",
            # No 'credential' key — Argo path
        }

        mock_sql_client = AsyncMock()
        activities.sql_client_class = lambda: mock_sql_client

        await activities._set_state(workflow_args)

        mock_get_credentials.assert_called_once_with("cred-argo")
        mock_sql_client.load.assert_called_once_with(
            {"username": "argo_user", "password": "argo_pass"}
        )

    @_patch_workflow_id()
    async def test_no_credential_guid_skips_loading(self, mock_wf_id, activities):
        """When credential_guid is absent, no credential loading occurs."""
        workflow_args = {"some_key": "some_value"}

        mock_sql_client = AsyncMock()
        activities.sql_client_class = lambda: mock_sql_client

        await activities._set_state(workflow_args)

        mock_sql_client.load.assert_not_called()
