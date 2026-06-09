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
    _create_store_from_binding_optional_with_put_attrs,
    _endpoint_is_aws,
    _find_broken_metadata_fields,
    _nonempty,
    _resolve_metadata_value,
    create_store_from_binding,
    create_store_from_binding_optional,
    create_store_from_binding_with_put_attrs,
    is_binding_configured,
)
from application_sdk.storage.errors import (
    StorageBindingBrokenError,
    StorageBindingNotFoundError,
    StorageConfigError,
)


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

    @patch("application_sdk.storage._credential_providers.AzureCredentialProvider")
    @patch("application_sdk.storage._credential_providers.CertificateCredential")
    @patch("obstore.store.AzureStore")
    def test_cert_only_does_not_trigger_multiple_modes_warning(
        self,
        mock_az_cls: MagicMock,
        mock_cert_cred: MagicMock,
        mock_az_prov: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Cert auth (tenant+client+cert, no secret) must not fire the multi-mode warning.

        Previously the WI classifier matched cert auth because it checks
        tenant_id + client_id + not client_secret, which is also true for cert.
        """
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
            },
        )
        with patch("application_sdk.storage.binding._get_logger") as mock_get_logger:
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger
            create_store_from_binding("objectstore", components_dir=components_dir)

        mock_logger.warning.assert_not_called()


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


class TestCreateStoreFromBindingOptional:
    def test_returns_none_when_component_absent(self, tmp_path: Path) -> None:
        """Returns None when no component with the given name exists."""
        result = create_store_from_binding_optional(
            "nonexistent", components_dir=tmp_path
        )
        assert result is None

    def test_reraises_for_misconfigured_component(self, tmp_path: Path) -> None:
        """Propagates StorageConfigError when the component exists but is misconfigured."""
        (tmp_path / "bad.yaml").write_text(
            "kind: Component\n"
            "metadata:\n"
            "  name: my-store\n"
            "spec:\n"
            "  type: bindings.unsupported.type\n"
            "  metadata: []\n"
        )
        with pytest.raises(StorageConfigError) as exc_info:
            create_store_from_binding_optional("my-store", components_dir=tmp_path)
        assert not isinstance(exc_info.value, StorageBindingNotFoundError)

    def test_binding_not_found_error_carries_binding_name(self, tmp_path: Path) -> None:
        """StorageBindingNotFoundError exposes the queried component name."""
        with pytest.raises(StorageBindingNotFoundError) as exc_info:
            create_store_from_binding("atlan-objectstore", components_dir=tmp_path)
        assert exc_info.value.binding_name == "atlan-objectstore"


# ---------------------------------------------------------------------------
# _create_store_from_binding_optional_with_put_attrs
# ---------------------------------------------------------------------------


class TestCreateStoreFromBindingOptionalWithPutAttrs:
    _PATCHED = (
        "application_sdk.storage.binding.create_store_from_binding_with_put_attrs"
    )

    def test_returns_none_when_component_absent(self, tmp_path: Path) -> None:
        """Returns (None, None) without raising when no Dapr component exists."""
        store, put_attrs = _create_store_from_binding_optional_with_put_attrs(
            "nonexistent", components_dir=tmp_path
        )
        assert store is None
        assert put_attrs is None

    def test_returns_none_and_warns_when_binding_broken(self, tmp_path: Path) -> None:
        """Returns (None, None) and logs a warning that includes the broken field names."""
        broken = StorageBindingBrokenError(
            "component has unresolvable fields",
            binding_name="my-store",
            broken_fields=["accessKey", "secretKey"],
        )
        with (
            patch(self._PATCHED, side_effect=broken),
            patch("application_sdk.storage.binding._get_logger") as mock_get_logger,
        ):
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger
            store, put_attrs = _create_store_from_binding_optional_with_put_attrs(
                "my-store", components_dir=tmp_path
            )
        assert store is None
        assert put_attrs is None
        mock_logger.warning.assert_called_once()
        warning_args = mock_logger.warning.call_args[0]
        warning_text = " ".join(str(a) for a in warning_args)
        assert "accessKey" in warning_text
        assert "secretKey" in warning_text

    def test_returns_store_and_put_attrs_on_success(self, tmp_path: Path) -> None:
        """Passes through (store, put_attrs) from the underlying factory unchanged."""
        mock_store = MagicMock()
        expected_put_attrs = {"Storage-Class": "STANDARD_IA"}
        with patch(self._PATCHED, return_value=(mock_store, expected_put_attrs)):
            store, put_attrs = _create_store_from_binding_optional_with_put_attrs(
                "my-store", components_dir=tmp_path
            )
        assert store is mock_store
        assert put_attrs == expected_put_attrs


# ---------------------------------------------------------------------------
# _find_broken_metadata_fields
# ---------------------------------------------------------------------------


class TestFindBrokenMetadataFields:
    def test_template_placeholder_flagged(self) -> None:
        items = [{"name": "bucket", "value": "{{tenant}}-data"}]
        assert _find_broken_metadata_fields(items) == ["bucket"]

    def test_clean_value_not_flagged(self) -> None:
        items = [{"name": "bucket", "value": "my-bucket"}]
        assert _find_broken_metadata_fields(items) == []

    def test_unresolved_secret_ref_flagged(self, monkeypatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        items = [
            {
                "name": "accessKey",
                "secretKeyRef": {"name": "MISSING_VAR", "key": "MISSING_VAR"},
            }
        ]
        assert _find_broken_metadata_fields(items) == ["accessKey"]

    def test_resolved_secret_ref_not_flagged(self, monkeypatch) -> None:
        monkeypatch.setenv("MY_KEY", "real-value")
        items = [
            {
                "name": "accessKey",
                "secretKeyRef": {"name": "MY_KEY", "key": "MY_KEY"},
            }
        ]
        assert _find_broken_metadata_fields(items) == []

    def test_empty_list_returns_empty(self) -> None:
        assert _find_broken_metadata_fields([]) == []

    def test_multiple_broken_fields_all_returned(self, monkeypatch) -> None:
        monkeypatch.delenv("MISSING_A", raising=False)
        monkeypatch.delenv("MISSING_B", raising=False)
        items = [
            {"name": "bucket", "value": "{{env}}-bucket"},
            {"name": "key", "secretKeyRef": {"key": "MISSING_A"}},
            {"name": "secret", "secretKeyRef": {"key": "MISSING_B"}},
        ]
        broken = _find_broken_metadata_fields(items)
        assert broken == ["bucket", "key", "secret"]


# ---------------------------------------------------------------------------
# StorageBindingBrokenError detection in create_store_from_binding
# ---------------------------------------------------------------------------


class TestStorageBindingBrokenError:
    def test_raises_for_template_placeholder(self, tmp_path: Path) -> None:
        """Non-local binding with {{template}} placeholder raises StorageBindingBrokenError."""
        components_dir = tmp_path / "components"
        components_dir.mkdir()
        doc = {
            "apiVersion": "dapr.io/v1alpha1",
            "kind": "Component",
            "metadata": {"name": "objectstore"},
            "spec": {
                "type": "bindings.aws.s3",
                "metadata": [
                    {"name": "bucket", "value": "{{tenant}}-data"},
                    {"name": "region", "value": "us-east-1"},
                ],
            },
        }
        import yaml

        (components_dir / "objectstore.yaml").write_text(yaml.dump(doc))

        with pytest.raises(StorageBindingBrokenError) as exc_info:
            create_store_from_binding("objectstore", components_dir=components_dir)

        assert "bucket" in exc_info.value.broken_fields
        assert exc_info.value.binding_name == "objectstore"

    def test_raises_for_unresolved_secret_ref(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Non-local binding with unresolved secretKeyRef raises StorageBindingBrokenError."""
        monkeypatch.delenv("UNSET_SECRET_KEY", raising=False)
        components_dir = tmp_path / "components"
        components_dir.mkdir()
        doc = {
            "apiVersion": "dapr.io/v1alpha1",
            "kind": "Component",
            "metadata": {"name": "objectstore"},
            "spec": {
                "type": "bindings.aws.s3",
                "metadata": [
                    {"name": "bucket", "value": "my-bucket"},
                    {
                        "name": "accessKey",
                        "secretKeyRef": {
                            "name": "UNSET_SECRET_KEY",
                            "key": "UNSET_SECRET_KEY",
                        },
                    },
                ],
            },
        }
        import yaml

        (components_dir / "objectstore.yaml").write_text(yaml.dump(doc))

        with pytest.raises(StorageBindingBrokenError) as exc_info:
            create_store_from_binding("objectstore", components_dir=components_dir)

        assert "accessKey" in exc_info.value.broken_fields

    def test_optional_returns_none_for_broken_component(self, tmp_path: Path) -> None:
        """create_store_from_binding_optional returns None for a broken component."""
        components_dir = tmp_path / "components"
        components_dir.mkdir()
        doc = {
            "apiVersion": "dapr.io/v1alpha1",
            "kind": "Component",
            "metadata": {"name": "objectstore"},
            "spec": {
                "type": "bindings.aws.s3",
                "metadata": [
                    {"name": "bucket", "value": "{{tenant}}-data"},
                ],
            },
        }
        import yaml

        (components_dir / "objectstore.yaml").write_text(yaml.dump(doc))

        result = create_store_from_binding_optional(
            "objectstore", components_dir=components_dir
        )
        assert result is None

    @patch("application_sdk.storage.factory.create_local_store")
    def test_local_storage_exempt_from_broken_check(
        self, mock_create_local: MagicMock, tmp_path: Path
    ) -> None:
        """bindings.localstorage with template placeholder is NOT flagged as broken."""
        mock_create_local.return_value = MagicMock()
        components_dir = tmp_path / "components"
        components_dir.mkdir()
        doc = {
            "apiVersion": "dapr.io/v1alpha1",
            "kind": "Component",
            "metadata": {"name": "localstore"},
            "spec": {
                "type": "bindings.localstorage",
                "metadata": [{"name": "rootPath", "value": "/data/{{tenant}}/storage"}],
            },
        }
        import yaml

        (components_dir / "localstore.yaml").write_text(yaml.dump(doc))

        # Must not raise StorageBindingBrokenError — local storage is exempt.
        create_store_from_binding("localstore", components_dir=components_dir)


# ---------------------------------------------------------------------------
# is_binding_configured
# ---------------------------------------------------------------------------


class TestIsBindingConfigured:
    def test_returns_false_when_absent(self, tmp_path: Path) -> None:
        assert is_binding_configured("nonexistent", components_dir=tmp_path) is False

    def test_returns_true_for_valid_s3_component(self, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.aws.s3", {"bucket": "b"}
        )
        assert (
            is_binding_configured("objectstore", components_dir=components_dir) is True
        )

    def test_returns_false_for_broken_component(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        components_dir.mkdir()
        doc = {
            "apiVersion": "dapr.io/v1alpha1",
            "kind": "Component",
            "metadata": {"name": "objectstore"},
            "spec": {
                "type": "bindings.aws.s3",
                "metadata": [{"name": "bucket", "value": "{{tenant}}-data"}],
            },
        }
        import yaml

        (components_dir / "objectstore.yaml").write_text(yaml.dump(doc))
        assert (
            is_binding_configured("objectstore", components_dir=components_dir) is False
        )

    def test_returns_false_for_unsupported_type(self, tmp_path: Path) -> None:
        components_dir = _write_component(
            tmp_path, "objectstore", "bindings.unknown", {}
        )
        assert (
            is_binding_configured("objectstore", components_dir=components_dir) is False
        )

    def test_local_storage_with_placeholder_returns_true(self, tmp_path: Path) -> None:
        """Local storage is exempt from broken-field checks."""
        components_dir = tmp_path / "components"
        components_dir.mkdir()
        doc = {
            "apiVersion": "dapr.io/v1alpha1",
            "kind": "Component",
            "metadata": {"name": "localstore"},
            "spec": {
                "type": "bindings.localstorage",
                "metadata": [{"name": "rootPath", "value": "/data/{{tenant}}"}],
            },
        }
        import yaml

        (components_dir / "localstore.yaml").write_text(yaml.dump(doc))
        assert (
            is_binding_configured("localstore", components_dir=components_dir) is True
        )


# ---------------------------------------------------------------------------
# _endpoint_is_aws
# ---------------------------------------------------------------------------


class TestEndpointIsAws:
    """Parametrised tests for the _endpoint_is_aws helper.

    The helper must correctly identify real-AWS endpoints and not raise for any
    input scheme form (https://, http://, scheme-less host:port, SDK URI forms).
    """

    @pytest.mark.parametrize(
        "endpoint",
        [
            # Standard public AWS
            "https://s3.amazonaws.com",
            "https://s3.us-east-1.amazonaws.com",
            "https://mybucket.s3.us-west-2.amazonaws.com",
            # AWS China
            "https://s3.cn-north-1.amazonaws.com.cn",
            "https://mybucket.s3.cn-northwest-1.amazonaws.com.cn",
            # FIPS / dualstack variants
            "https://s3-fips.us-east-1.amazonaws.com",
            "https://s3.dualstack.us-east-1.amazonaws.com",
            # http scheme (e.g. LocalStack)
            "http://s3.amazonaws.com",
            # Trailing root dot (FQDN form) — must not break AWS detection
            "https://s3.amazonaws.com.",
            "https://s3.cn-north-1.amazonaws.com.cn.",
            # Empty / absent endpoint → real AWS credential chain
            "",
        ],
    )
    def test_aws_endpoints_return_true(self, endpoint: str) -> None:
        assert _endpoint_is_aws(endpoint) is True

    @pytest.mark.parametrize(
        "endpoint",
        [
            # Cloudflare R2
            "https://abc123.r2.cloudflarestorage.com",
            # Backblaze B2
            "https://s3.us-west-002.backblazeb2.com",
            # GCS S3-interop
            "https://storage.googleapis.com",
            # MinIO with scheme
            "http://minio:9000",
            "https://minio.example.com:9000",
            # Scheme-less host:port
            "minio.local:9000",
            # SDK-internal URI forms
            "s3://my-bucket",
            "objectstore://host",
            "adls://account.dfs.core.windows.net",
            # Ceph RADOSGW
            "https://ceph.internal.example.com",
        ],
    )
    def test_non_aws_endpoints_return_false(self, endpoint: str) -> None:
        assert _endpoint_is_aws(endpoint) is False

    @pytest.mark.parametrize(
        "endpoint",
        [
            # Pathological inputs — must not raise
            "not-a-url",
            "///",
            "http://",
            ":9000",
        ],
    )
    def test_no_exception_on_pathological_input(self, endpoint: str) -> None:
        """_endpoint_is_aws must never raise regardless of input form."""
        result = _endpoint_is_aws(endpoint)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# S3 compatibility knobs (tagging, pass-through fields, storageClass)
# ---------------------------------------------------------------------------


class TestS3CompatibilityKnobs:
    """Tests for the Section-C gap fixes in _build_s3_config."""

    # --- Tagging auto-detect ------------------------------------------------

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://abc123.r2.cloudflarestorage.com",
            "https://s3.us-west-002.backblazeb2.com",
            "https://storage.googleapis.com",
            "http://minio:9000",
            "https://minio.example.com",
        ],
    )
    @patch("obstore.store.S3Store")
    def test_non_aws_endpoint_auto_disables_tagging(
        self, mock_s3_cls: MagicMock, tmp_path: Path, endpoint: str
    ) -> None:
        components_dir = _write_component(
            tmp_path / endpoint.replace("/", "_").replace(":", "_"),
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "endpoint": endpoint},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        config = mock_s3_cls.call_args.kwargs["config"]
        assert (
            config.get("aws_disable_tagging") == "true"
        ), f"Expected disable_tagging for {endpoint!r}"

    @pytest.mark.parametrize(
        "endpoint",
        [
            "",  # no endpoint → real AWS
            "https://s3.amazonaws.com",
            "https://s3.us-east-1.amazonaws.com",
            "https://s3.cn-north-1.amazonaws.com.cn",
        ],
    )
    @patch("obstore.store.S3Store")
    def test_aws_endpoint_does_not_disable_tagging(
        self, mock_s3_cls: MagicMock, tmp_path: Path, endpoint: str
    ) -> None:
        meta: dict = {"bucket": "b"}
        if endpoint:
            meta["endpoint"] = endpoint
        components_dir = _write_component(
            tmp_path / endpoint.replace("/", "_").replace(":", "_"),
            "objectstore",
            "bindings.aws.s3",
            meta,
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        config = mock_s3_cls.call_args.kwargs.get("config") or {}
        assert (
            "aws_disable_tagging" not in config
        ), f"Should not disable tagging for AWS endpoint {endpoint!r}"

    @patch("obstore.store.S3Store")
    def test_explicit_disable_tagging_true_overrides_aws_endpoint(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        """disableTagging=true wins even on a real AWS endpoint."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {
                "bucket": "b",
                "endpoint": "https://s3.amazonaws.com",
                "disableTagging": "true",
            },
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        config = mock_s3_cls.call_args.kwargs["config"]
        assert config.get("aws_disable_tagging") == "true"

    @patch("obstore.store.S3Store")
    def test_explicit_disable_tagging_false_overrides_non_aws_auto_detect(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        """disableTagging=false wins over the non-AWS auto-detect rule."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "endpoint": "http://minio:9000", "disableTagging": "false"},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        config = mock_s3_cls.call_args.kwargs.get("config") or {}
        # Explicit false → key absent (obstore default applies)
        assert "aws_disable_tagging" not in config

    # --- Pass-through override fields --------------------------------------

    @patch("obstore.store.S3Store")
    def test_conditional_put_pass_through(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "conditionalPut": "etag"},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        config = mock_s3_cls.call_args.kwargs["config"]
        assert config.get("aws_conditional_put") == "etag"

    @patch("obstore.store.S3Store")
    def test_copy_if_not_exists_pass_through(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "copyIfNotExists": "multipart"},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        config = mock_s3_cls.call_args.kwargs["config"]
        assert config.get("aws_copy_if_not_exists") == "multipart"

    @patch("obstore.store.S3Store")
    def test_checksum_algorithm_pass_through(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "checksumAlgorithm": "SHA256"},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        config = mock_s3_cls.call_args.kwargs["config"]
        assert config.get("aws_checksum_algorithm") == "SHA256"

    @patch("obstore.store.S3Store")
    def test_sse_kms_pair_pass_through(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {
                "bucket": "b",
                "serverSideEncryption": "aws:kms",
                "sseKmsKeyId": "arn:aws:kms:us-east-1:123456789012:key/abc",
            },
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        config = mock_s3_cls.call_args.kwargs["config"]
        assert config.get("aws_server_side_encryption") == "aws:kms"
        assert config.get("aws_sse_kms_key_id") == (
            "arn:aws:kms:us-east-1:123456789012:key/abc"
        )

    @patch("obstore.store.S3Store")
    def test_absent_override_fields_leave_config_untouched(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        """Existing configs with none of the new fields must be unchanged."""
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "region": "us-east-1", "accessKey": "k", "secretKey": "s"},
        )
        mock_s3_cls.return_value = MagicMock()
        create_store_from_binding("objectstore", components_dir=components_dir)
        config = mock_s3_cls.call_args.kwargs.get("config") or {}
        for key in (
            "aws_conditional_put",
            "aws_copy_if_not_exists",
            "aws_checksum_algorithm",
            "aws_server_side_encryption",
            "aws_sse_kms_key_id",
            "aws_disable_tagging",
        ):
            assert key not in config, f"Unexpected key {key!r} in config"

    # --- storageClass / put attributes -------------------------------------

    @patch("obstore.store.S3Store")
    def test_storage_class_produces_put_attributes(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "storageClass": "STANDARD_IA"},
        )
        mock_s3_cls.return_value = MagicMock()
        store, put_attrs = create_store_from_binding_with_put_attrs(
            "objectstore", components_dir=components_dir
        )
        assert put_attrs == {"Storage-Class": "STANDARD_IA"}

    @patch("obstore.store.S3Store")
    def test_absent_storage_class_returns_none_put_attributes(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "region": "us-east-1"},
        )
        mock_s3_cls.return_value = MagicMock()
        store, put_attrs = create_store_from_binding_with_put_attrs(
            "objectstore", components_dir=components_dir
        )
        assert put_attrs is None

    @patch("obstore.store.S3Store")
    def test_storage_class_glacier_ir(
        self, mock_s3_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.aws.s3",
            {"bucket": "b", "storageClass": "GLACIER_IR"},
        )
        mock_s3_cls.return_value = MagicMock()
        _, put_attrs = create_store_from_binding_with_put_attrs(
            "objectstore", components_dir=components_dir
        )
        assert put_attrs == {"Storage-Class": "GLACIER_IR"}

    @patch("obstore.store.AzureStore")
    def test_azure_storage_class_produces_put_attributes(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {
                "containerName": "c",
                "accountName": "acct",
                "accountKey": "key==",
                "storageClass": "Cool",
            },
        )
        mock_az_cls.return_value = MagicMock()
        _, put_attrs = create_store_from_binding_with_put_attrs(
            "objectstore", components_dir=components_dir
        )
        assert put_attrs == {"x-ms-access-tier": "Cool"}

    @patch("obstore.store.AzureStore")
    def test_azure_absent_storage_class_returns_none_put_attributes(
        self, mock_az_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.azure.blobstorage",
            {"containerName": "c", "accountName": "acct", "accountKey": "key=="},
        )
        mock_az_cls.return_value = MagicMock()
        _, put_attrs = create_store_from_binding_with_put_attrs(
            "objectstore", components_dir=components_dir
        )
        assert put_attrs is None

    @patch("obstore.store.GCSStore")
    def test_gcs_storage_class_produces_put_attributes(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.gcp.bucket",
            {"bucket": "b", "storageClass": "NEARLINE"},
        )
        mock_gcs_cls.return_value = MagicMock()
        _, put_attrs = create_store_from_binding_with_put_attrs(
            "objectstore", components_dir=components_dir
        )
        assert put_attrs == {"X-Goog-Storage-Class": "NEARLINE"}

    @patch("obstore.store.GCSStore")
    def test_gcs_absent_storage_class_returns_none_put_attributes(
        self, mock_gcs_cls: MagicMock, tmp_path: Path
    ) -> None:
        components_dir = _write_component(
            tmp_path,
            "objectstore",
            "bindings.gcp.bucket",
            {"bucket": "b"},
        )
        mock_gcs_cls.return_value = MagicMock()
        _, put_attrs = create_store_from_binding_with_put_attrs(
            "objectstore", components_dir=components_dir
        )
        assert put_attrs is None
