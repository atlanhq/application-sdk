"""Unit tests for create_store_from_binding."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import orjson
import pytest
import yaml

from application_sdk.storage.binding import (
    _AZURE_AUTHORITY_HOSTS,
    GCS_SERVICE_ACCOUNT_FIELDS,
    _coerce_bool,
    _nonempty,
    _resolve_metadata_value,
    create_store_from_binding,
)
from application_sdk.storage.errors import StorageConfigError


def _write_component(
    tmp_path: Path, name: str, binding_type: str, metadata: dict
) -> Path:
    """Write a Dapr component YAML and return the components dir."""
    components_dir = tmp_path / "components"
    components_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "apiVersion": "dapr.io/v1alpha1",
        "kind": "Component",
        "metadata": {"name": name},
        "spec": {
            "type": binding_type,
            "metadata": [{"name": k, "value": v} for k, v in metadata.items()],
        },
    }
    (components_dir / f"{name}.yaml").write_text(yaml.dump(doc))
    return components_dir


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestNonempty:
    def test_first_nonempty_wins(self):
        assert _nonempty({"a": "", "b": "val"}, "a", "b") == "val"

    def test_returns_empty_when_all_missing(self):
        assert _nonempty({"a": ""}, "a", "z") == ""

    def test_empty_string_treated_as_missing(self):
        assert _nonempty({"region": ""}, "region") == ""

    def test_first_key_wins_if_nonempty(self):
        assert _nonempty({"a": "first", "b": "second"}, "a", "b") == "first"


class TestCoerceBool:
    @pytest.mark.parametrize("val", ["true", "True", "TRUE", "1", "yes", "YES"])
    def test_truthy_values(self, val):
        assert _coerce_bool(val) is True

    @pytest.mark.parametrize("val", ["false", "False", "0", "no", "", "maybe"])
    def test_falsy_values(self, val):
        assert _coerce_bool(val) is False


# ---------------------------------------------------------------------------
# _resolve_metadata_value
# ---------------------------------------------------------------------------


class TestResolveMetadataValue:
    def test_plain_value(self):
        assert (
            _resolve_metadata_value({"name": "bucket", "value": "my-bucket"})
            == "my-bucket"
        )

    def test_secret_key_ref_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "secret-value")
        item = {
            "name": "accessKey",
            "secretKeyRef": {"name": "MY_SECRET", "key": "MY_SECRET"},
        }
        assert _resolve_metadata_value(item) == "secret-value"

    def test_key_takes_precedence_over_name(self, monkeypatch):
        monkeypatch.setenv("KEY_VAR", "from-key")
        monkeypatch.setenv("NAME_VAR", "from-name")
        item = {"name": "x", "secretKeyRef": {"name": "NAME_VAR", "key": "KEY_VAR"}}
        assert _resolve_metadata_value(item) == "from-key"

    def test_fallback_to_name_when_no_key(self, monkeypatch):
        monkeypatch.setenv("NAME_VAR", "from-name")
        item = {"name": "x", "secretKeyRef": {"name": "NAME_VAR"}}
        assert _resolve_metadata_value(item) == "from-name"

    def test_missing_env_returns_empty(self):
        item = {"name": "x", "secretKeyRef": {"key": "NONEXISTENT_VAR"}}
        assert _resolve_metadata_value(item) == ""

    def test_no_value_no_ref_returns_empty(self):
        assert _resolve_metadata_value({"name": "x"}) == ""

    def test_value_takes_precedence_over_secret_ref(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "from-env")
        item = {
            "name": "x",
            "value": "from-value",
            "secretKeyRef": {"key": "MY_SECRET"},
        }
        assert _resolve_metadata_value(item) == "from-value"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestBindingErrors:
    def test_missing_component_raises(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        components_dir.mkdir()
        (components_dir / "empty.yaml").write_text(
            yaml.dump(
                {
                    "kind": "Component",
                    "metadata": {"name": "other"},
                    "spec": {"type": "bindings.gcs"},
                }
            )
        )
        with pytest.raises(StorageConfigError, match="No Dapr component named"):
            create_store_from_binding("nonexistent", components_dir=components_dir)

    def test_unsupported_type_raises(self, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.unknown", {}
        )
        with pytest.raises(StorageConfigError, match="Unsupported binding type"):
            create_store_from_binding("objectstore", components_dir=components_dir)


# ---------------------------------------------------------------------------
# S3 tests
# ---------------------------------------------------------------------------

FULL_SA_META = {
    "bucket": "my-bucket",
    "type": "service_account",
    "project_id": "my-project",
    "private_key_id": "key-id-123",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\\nMIIE...\\n-----END RSA PRIVATE KEY-----\\n",
    "client_email": "sa@my-project.iam.gserviceaccount.com",
    "client_id": "123456789",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/sa",
    "universe_domain": "googleapis.com",
}


class TestS3StaticCredentials:
    @patch("obstore.store.S3Store")
    def test_access_and_secret_key_translated(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {
                "bucket": "b",
                "region": "us-east-1",
                "accessKey": "AK",
                "secretKey": "SK",
            },
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_s3_cls.call_args.kwargs["config"]
        assert config["aws_access_key_id"] == "AK"
        assert config["aws_secret_access_key"] == "SK"
        assert config["aws_region"] == "us-east-1"

    @patch("obstore.store.S3Store")
    def test_session_token_translated(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {
                "bucket": "b",
                "region": "us-east-1",
                "accessKey": "AK",
                "secretKey": "SK",
                "sessionToken": "TOKEN",
            },
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_s3_cls.call_args.kwargs["config"]
        assert config["aws_session_token"] == "TOKEN"


class TestS3EmptyConfigHardening:
    @patch("obstore.store.S3Store")
    def test_bucket_only_passes_config_none(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Bucket+region only → no credential keys emitted, IRSA / instance-profile activates."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "region": "us-east-1"},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_s3_cls.call_args.kwargs["config"] or {}
        assert "aws_access_key_id" not in config
        assert "aws_secret_access_key" not in config

    @patch("obstore.store.S3Store")
    def test_empty_string_region_treated_as_missing(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        """An empty-string region must not be emitted into the obstore config."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "region": ""},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_s3_cls.call_args.kwargs["config"]
        assert config is None or "aws_region" not in (config or {})


class TestS3BehaviorKnobs:
    @patch("obstore.store.S3Store")
    def test_endpoint_and_force_path_style(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {
                "bucket": "b",
                "region": "us-east-1",
                "endpoint": "http://minio:9000",
                "forcePathStyle": "true",
            },
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_s3_cls.call_args.kwargs["config"]
        assert config["aws_endpoint"] == "http://minio:9000"
        assert config["aws_virtual_hosted_style_request"] == "false"
        assert mock_s3_cls.call_args.kwargs["client_options"]["user_agent"] == (
            "aws-sdk-go-v2 atlan-application-sdk"
        )

    @patch("obstore.store.S3Store")
    def test_force_path_style_case_insensitive(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        for val in ("True", "TRUE", "1"):
            components_dir = _write_component(
                tmp_path / val,
                "objectstore",
                "bindings.aws.s3",
                {"bucket": "b", "endpoint": "http://x", "forcePathStyle": val},
            )
            mock_s3_cls.return_value = MagicMock()
            create_store_from_binding("objectstore", components_dir=components_dir)
            config = mock_s3_cls.call_args.kwargs["config"]
            assert (
                config.get("aws_virtual_hosted_style_request") == "false"
            ), f"failed for {val!r}"

    @patch("obstore.store.S3Store")
    def test_disable_ssl_sets_allow_http(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "disableSSL": "true"},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        client_options = mock_s3_cls.call_args.kwargs["client_options"]
        assert client_options["allow_http"] is True

    @patch("obstore.store.S3Store")
    def test_insecure_ssl_sets_allow_invalid_certs(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "insecureSSL": "true"},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        client_options = mock_s3_cls.call_args.kwargs["client_options"]
        assert client_options["allow_invalid_certificates"] is True

    @patch("obstore.store.S3Store")
    def test_no_endpoint_no_path_style_no_user_agent(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "region": "us-east-1"},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_s3_cls.call_args.kwargs.get("config") or {}
        assert "aws_endpoint" not in config
        assert "aws_virtual_hosted_style_request" not in config
        client_options = mock_s3_cls.call_args.kwargs["client_options"]
        assert client_options.get("timeout") == "90s"
        assert client_options.get("user_agent", "").startswith("atlan-application-sdk")


class TestS3AssumeRole:
    @patch("application_sdk.storage._credential_providers.StsCredentialProvider")
    @patch("boto3.Session")
    @patch("obstore.store.S3Store")
    def test_assume_role_sets_credential_provider(
        self,
        mock_s3_cls: MagicMock,
        mock_session_cls: MagicMock,
        mock_sts_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_s3_cls.return_value = MagicMock()
        mock_sts_cls.return_value = MagicMock()
        mock_session_cls.return_value = MagicMock()

        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {
                "bucket": "b",
                "region": "us-east-1",
                "assumeRoleArn": "arn:aws:iam::123:role/MyRole",
                "sessionName": "my-session",
            },
        )
        create_store_from_binding("objectstore", components_dir=components_dir)

        mock_sts_cls.assert_called_once()
        call_kwargs = mock_s3_cls.call_args.kwargs
        assert call_kwargs["credential_provider"] is not None
        config = call_kwargs.get("config") or {}
        assert "aws_access_key_id" not in config
        assert "aws_secret_access_key" not in config

    def test_trust_anchor_arn_raises_config_error(self, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {
                "bucket": "b",
                "trustAnchorArn": "arn:aws:rolesanywhere::123:trust-anchor/abc",
            },
        )
        with pytest.raises(StorageConfigError, match="IAM Roles Anywhere"):
            create_store_from_binding("objectstore", components_dir=components_dir)

    @patch("application_sdk.storage._credential_providers.StsCredentialProvider")
    @patch("boto3.Session")
    @patch("obstore.store.S3Store")
    def test_partial_base_creds_warns_and_ignores(
        self,
        mock_s3_cls: MagicMock,
        mock_session_cls: MagicMock,
        mock_sts_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Only accessKey (no secretKey) with assumeRoleArn → warning, both dropped."""
        mock_s3_cls.return_value = MagicMock()
        mock_sts_cls.return_value = MagicMock()
        mock_session_cls.return_value = MagicMock()

        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {
                "bucket": "b",
                "assumeRoleArn": "arn:aws:iam::123:role/R",
                "accessKey": "AK",
                "sessionToken": "STOKEN",
                # secretKey intentionally omitted — triggers the partial-creds path
            },
        )
        with patch("application_sdk.storage.binding._get_logger") as mock_get_logger:
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger
            create_store_from_binding("objectstore", components_dir=components_dir)

        mock_logger.warning.assert_called_once()
        warning_msg = mock_logger.warning.call_args.args[0]
        assert "accessKey" in warning_msg or "base credentials" in warning_msg

        # All three base creds (key, secret, token) must be absent from the Session call
        session_kwargs = mock_session_cls.call_args.kwargs
        assert "aws_access_key_id" not in session_kwargs
        assert "aws_secret_access_key" not in session_kwargs
        assert "aws_session_token" not in session_kwargs


class TestS3SecretKeyRef:
    @patch("obstore.store.S3Store")
    def test_secret_key_ref_resolves_credentials(
        self, mock_s3_cls: MagicMock, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ATLAN_AUTH_CLIENT_ID", "test-access-key")
        monkeypatch.setenv("ATLAN_AUTH_CLIENT_SECRET", "test-secret-key")

        components_dir = tmp_path / "components"
        components_dir.mkdir()
        doc = {
            "apiVersion": "dapr.io/v1alpha1",
            "kind": "Component",
            "metadata": {"name": "objectstore"},
            "spec": {
                "type": "bindings.aws.s3",
                "metadata": [
                    {"name": "bucket", "value": "test-bucket"},
                    {"name": "region", "value": "us-east-1"},
                    {
                        "name": "accessKey",
                        "secretKeyRef": {
                            "name": "ATLAN_AUTH_CLIENT_ID",
                            "key": "ATLAN_AUTH_CLIENT_ID",
                        },
                    },
                    {
                        "name": "secretKey",
                        "secretKeyRef": {
                            "name": "ATLAN_AUTH_CLIENT_SECRET",
                            "key": "ATLAN_AUTH_CLIENT_SECRET",
                        },
                    },
                ],
            },
        }
        (components_dir / "objectstore.yaml").write_text(yaml.dump(doc))
        mock_s3_cls.return_value = MagicMock()

        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_s3_cls.call_args.kwargs["config"]
        assert config["aws_access_key_id"] == "test-access-key"
        assert config["aws_secret_access_key"] == "test-secret-key"


# ---------------------------------------------------------------------------
# Azure / ADLS tests
# ---------------------------------------------------------------------------


class TestAzureAccountKey:
    @patch("obstore.store.AzureStore")
    def test_account_key_auth(self, mock_az_cls: MagicMock, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "myaccount",
                "containerName": "mycontainer",
                "accountKey": "KEY==",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        kw = mock_az_cls.call_args.kwargs
        assert kw["container_name"] == "mycontainer"
        assert kw["config"]["azure_storage_account_name"] == "myaccount"
        assert kw["config"]["azure_storage_account_key"] == "KEY=="
        assert "azure_storage_tenant_id" not in kw["config"]


class TestAzureSASToken:
    @patch("obstore.store.AzureStore")
    def test_sas_token_key(self, mock_az_cls: MagicMock, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "sasToken": "sv=2023&sig=abc",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_sas_key"] == "sv=2023&sig=abc"
        assert "azure_storage_account_key" not in config

    @patch("obstore.store.AzureStore")
    def test_sas_key_alias(self, mock_az_cls: MagicMock, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "sasKey": "sv=2023&sig=xyz",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_sas_key"] == "sv=2023&sig=xyz"


class TestAzureServicePrincipal:
    @patch("obstore.store.AzureStore")
    def test_sp_client_secret(self, mock_az_cls: MagicMock, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "azureTenantId": "tenant-123",
                "azureClientId": "client-456",
                "azureClientSecret": "secret-789",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_tenant_id"] == "tenant-123"
        assert config["azure_storage_client_id"] == "client-456"
        assert config["azure_storage_client_secret"] == "secret-789"
        assert "azure_storage_account_key" not in config

    @patch("obstore.store.AzureStore")
    def test_sp_with_unprefixed_aliases(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        """tenantId / clientId (no azure prefix) should work identically."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "tenantId": "tenant-123",
                "clientId": "client-456",
                "azureClientSecret": "secret-789",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_tenant_id"] == "tenant-123"
        assert config["azure_storage_client_id"] == "client-456"
        assert config["azure_storage_client_secret"] == "secret-789"


class TestAzureWorkloadIdentity:
    @patch("obstore.store.AzureStore")
    def test_tenant_and_client_only_is_workload_identity(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        """tenantId + clientId without a secret → AKS WI path."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "azureTenantId": "tid",
                "azureClientId": "cid",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_tenant_id"] == "tid"
        assert config["azure_storage_client_id"] == "cid"
        assert "azure_storage_client_secret" not in config
        assert "azure_storage_account_key" not in config

    @patch("obstore.store.AzureStore")
    def test_federated_token_file_in_metadata(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "azureTenantId": "tid",
                "azureClientId": "cid",
                "azureFederatedTokenFile": "/var/run/secrets/token",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_federated_token_file"] == "/var/run/secrets/token"


class TestAzureManagedIdentity:
    @patch("obstore.store.AzureStore")
    def test_client_id_only_is_user_assigned_mi(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {"accountName": "acct", "containerName": "ctr", "azureClientId": "cid"},
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_client_id"] == "cid"
        assert "azure_storage_tenant_id" not in config
        assert "azure_storage_client_secret" not in config

    @patch("obstore.store.AzureStore")
    def test_no_creds_passes_config_none(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        """accountName + containerName only → empty az_config → config=None (DefaultAzureCredential)."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {"accountName": "", "containerName": "ctr"},
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config is None or not config


class TestAzureEnvironment:
    @pytest.mark.parametrize(
        "env_name,expected_host",
        list(_AZURE_AUTHORITY_HOSTS.items()),
    )
    @patch("obstore.store.AzureStore")
    def test_known_environment_values(
        self, mock_az_cls: MagicMock, env_name: str, expected_host: str, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path / env_name,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "azureEnvironment": env_name,
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_authority_host"] == expected_host

    def test_unknown_environment_raises(self, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "azureEnvironment": "AzureMars",
            },
        )
        with pytest.raises(StorageConfigError, match="Unknown azureEnvironment"):
            create_store_from_binding("objectstore", components_dir=components_dir)


class TestAzureEndpointAndEmulator:
    @patch("obstore.store.AzureStore")
    def test_custom_endpoint_translated(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "devstoreaccount1",
                "containerName": "ctr",
                "accountKey": "KEY==",
                "endpoint": "http://127.0.0.1:10000",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_endpoint"] == "http://127.0.0.1:10000"

    @patch("obstore.store.AzureStore")
    def test_use_emulator_flag(self, mock_az_cls: MagicMock, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "devstoreaccount1",
                "containerName": "ctr",
                "useEmulator": "true",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert config["azure_storage_use_emulator"] == "true"


class TestAzureCertificateProvider:
    @patch("application_sdk.storage._credential_providers.AzureCredentialProvider")
    @patch("application_sdk.storage._credential_providers.CertificateCredential")
    @patch("obstore.store.AzureStore")
    def test_cert_file_creates_provider(
        self,
        mock_az_cls: MagicMock,
        mock_cert_cred: MagicMock,
        mock_az_prov: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_az_cls.return_value = MagicMock()
        mock_cert_cred.return_value = MagicMock()
        mock_az_prov.return_value = MagicMock()

        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "azureTenantId": "tid",
                "azureClientId": "cid",
                "azureCertificateFile": "/certs/app.pfx",
                "azureCertificatePassword": "certpass",
            },
        )
        create_store_from_binding("objectstore", components_dir=components_dir)

        mock_cert_cred.assert_called_once()
        cert_kwargs = mock_cert_cred.call_args.kwargs
        assert cert_kwargs["tenant_id"] == "tid"
        assert cert_kwargs["client_id"] == "cid"
        assert cert_kwargs["certificate_path"] == "/certs/app.pfx"
        assert cert_kwargs["password"] == "certpass"
        mock_az_prov.assert_called_once()
        kw = mock_az_cls.call_args.kwargs
        assert kw["credential_provider"] is not None
        assert "azure_storage_account_key" not in (kw.get("config") or {})

    def test_cert_without_tenant_and_client_id_raises(self, tmp_path: Path) -> None:
        """azureCertificateFile without azureTenantId/azureClientId → StorageConfigError."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "azureCertificateFile": "/certs/app.pfx",
                # tenant and client IDs intentionally omitted
            },
        )
        with pytest.raises(StorageConfigError, match="azureTenantId and azureClientId"):
            create_store_from_binding("objectstore", components_dir=components_dir)

    @patch("obstore.store.AzureStore")
    def test_multiple_auth_modes_logs_warning(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        """accountKey + service-principal fields both set → warning logged."""
        mock_az_cls.return_value = MagicMock()
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "accountKey": "key123",
                "azureTenantId": "tid",
                "azureClientId": "cid",
                "azureClientSecret": "secret",
            },
        )
        with patch("application_sdk.storage.binding._get_logger") as mock_get_logger:
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger
            create_store_from_binding("objectstore", components_dir=components_dir)

        mock_logger.warning.assert_called_once()
        assert "multiple auth modes" in mock_logger.warning.call_args.args[0]

        # accountKey takes priority — key must appear in config
        config = mock_az_cls.call_args.kwargs.get("config") or {}
        assert config.get("azure_storage_account_key") == "key123"


class TestAzureMsiExtras:
    @patch("obstore.store.AzureStore")
    def test_msi_endpoint_and_resource_id(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "accountName": "acct",
                "containerName": "ctr",
                "msiEndpoint": "http://169.254.169.254/metadata/identity/oauth2/token",
                "msiResourceId": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/myid",
            },
        )
        mock_az_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_az_cls.call_args.kwargs["config"]
        assert "azure_storage_msi_endpoint" in config
        assert "azure_storage_msi_resource_id" in config


# ---------------------------------------------------------------------------
# GCS tests
# ---------------------------------------------------------------------------


class TestGCSStoreCredentials:
    @patch("obstore.store.GCSStore")
    def test_full_sa_metadata_passed_as_service_account_key(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.gcs", FULL_SA_META
        )
        mock_gcs_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        call_kwargs = mock_gcs_cls.call_args
        assert call_kwargs.kwargs["bucket"] == "my-bucket"
        config = call_kwargs.kwargs["config"]
        assert "google_service_account_key" in config
        sa_json = orjson.loads(config["google_service_account_key"])
        assert sa_json["project_id"] == "my-project"
        assert sa_json["client_email"] == "sa@my-project.iam.gserviceaccount.com"
        assert sa_json["universe_domain"] == "googleapis.com"

    @patch("obstore.store.GCSStore")
    def test_project_id_only_falls_through_to_adc(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """bucket + project_id only → config=None so WIF / ADC takes over (PR #1897)."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.gcs",
            {"bucket": "prod-bucket", "project_id": "my-project"},
        )
        mock_gcs_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_gcs_cls.call_args.kwargs["config"]
        assert config is None
        assert mock_gcs_cls.call_args.kwargs["bucket"] == "prod-bucket"

    @patch("obstore.store.GCSStore")
    def test_identifier_fields_without_private_key_fall_through_to_adc(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """project_id + client_email without private_key → config=None (not a partial SA JSON)."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.gcs",
            {
                "bucket": "b",
                "project_id": "proj",
                "client_email": "sa@proj.iam.gserviceaccount.com",
            },
        )
        mock_gcs_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_gcs_cls.call_args.kwargs["config"]
        assert config is None

    @patch("obstore.store.GCSStore")
    def test_no_sa_fields_passes_config_none(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """bucket only → config=None."""
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.gcs", {"bucket": "dev-bucket"}
        )
        mock_gcs_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        assert mock_gcs_cls.call_args.kwargs["config"] is None

    @patch("obstore.store.GCSStore")
    def test_private_key_newlines_normalized(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Escaped \\n from Helm templating should become real newlines."""
        meta = {
            "bucket": "b",
            "type": "service_account",
            "private_key": "-----BEGIN KEY-----\\ndata\\n-----END KEY-----\\n",
        }
        components_dir = _write_component(tmp_path, "objectstore", "bindings.gcs", meta)
        mock_gcs_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        sa_json = orjson.loads(
            mock_gcs_cls.call_args.kwargs["config"]["google_service_account_key"]
        )
        assert "\\n" not in sa_json["private_key"]
        assert "\n" in sa_json["private_key"]

    @patch("obstore.store.GCSStore")
    def test_bindings_gcp_bucket_type_also_works(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Both 'bindings.gcs' and 'bindings.gcp.bucket' should route to GCS."""
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.gcp.bucket", {"bucket": "alt-bucket"}
        )
        mock_gcs_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        assert mock_gcs_cls.call_args.kwargs["bucket"] == "alt-bucket"

    @patch("obstore.store.GCSStore")
    def test_private_key_id_alone_triggers_sa_json(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """private_key_id without private_key also triggers SA JSON build."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.gcs",
            {"bucket": "b", "private_key_id": "kid-123", "client_email": "sa@x.com"},
        )
        mock_gcs_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_gcs_cls.call_args.kwargs["config"]
        assert config is not None
        assert "google_service_account_key" in config

    @patch("obstore.store.GCSStore")
    def test_config_key_is_google_service_account_key(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Verify the obstore config key matches cloud.py ('google_service_account_key')."""
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.gcs", FULL_SA_META
        )
        mock_gcs_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)

        config = mock_gcs_cls.call_args.kwargs["config"]
        assert "google_service_account_key" in config
        assert "service_account_key" not in config


class TestGCSServiceAccountFieldsConstant:
    def test_contains_required_fields(self) -> None:
        required = {
            "type",
            "project_id",
            "private_key_id",
            "private_key",
            "client_email",
            "client_id",
            "auth_uri",
            "token_uri",
            "auth_provider_x509_cert_url",
            "client_x509_cert_url",
            "universe_domain",
        }
        assert required == set(GCS_SERVICE_ACCOUNT_FIELDS)
