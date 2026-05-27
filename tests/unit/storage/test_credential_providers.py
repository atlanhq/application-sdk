"""Unit tests for _credential_providers (mocked dependencies)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from application_sdk.storage.errors import StorageConfigError


class TestMakeS3AssumeRoleProvider:
    @patch("obstore.auth.boto3.StsCredentialProvider")
    @patch("boto3.Session")
    def test_returns_sts_credential_provider(
        self, mock_session_cls: MagicMock, mock_sts_cls: MagicMock
    ) -> None:
        from application_sdk.storage._credential_providers import (
            make_s3_assume_role_provider,
        )

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_provider = MagicMock()
        mock_sts_cls.return_value = mock_provider

        result = make_s3_assume_role_provider(
            role_arn="arn:aws:iam::123:role/MyRole",
            session_name="my-session",
            region="us-east-1",
        )

        mock_session_cls.assert_called_once_with(region_name="us-east-1")
        mock_sts_cls.assert_called_once_with(
            mock_session,
            RoleArn="arn:aws:iam::123:role/MyRole",
            RoleSessionName="my-session",
        )
        assert result is mock_provider

    @patch("obstore.auth.boto3.StsCredentialProvider")
    @patch("boto3.Session")
    def test_base_credentials_passed_to_session(
        self, mock_session_cls: MagicMock, mock_sts_cls: MagicMock
    ) -> None:
        from application_sdk.storage._credential_providers import (
            make_s3_assume_role_provider,
        )

        mock_session_cls.return_value = MagicMock()
        mock_sts_cls.return_value = MagicMock()

        make_s3_assume_role_provider(
            role_arn="arn:aws:iam::123:role/MyRole",
            base_access_key="AK",
            base_secret_key="SK",
            region="eu-west-1",
        )

        session_kwargs = mock_session_cls.call_args.kwargs
        assert session_kwargs["aws_access_key_id"] == "AK"
        assert session_kwargs["aws_secret_access_key"] == "SK"
        assert session_kwargs["region_name"] == "eu-west-1"

    @patch("obstore.auth.boto3.StsCredentialProvider")
    @patch("boto3.Session")
    def test_external_id_forwarded_to_sts(
        self, mock_session_cls: MagicMock, mock_sts_cls: MagicMock
    ) -> None:
        from application_sdk.storage._credential_providers import (
            make_s3_assume_role_provider,
        )

        mock_session_cls.return_value = MagicMock()
        mock_sts_cls.return_value = MagicMock()

        make_s3_assume_role_provider(
            role_arn="arn:aws:iam::123:role/R",
            external_id="ext-123",
        )

        sts_kwargs = mock_sts_cls.call_args.kwargs
        assert sts_kwargs["ExternalId"] == "ext-123"

    @patch("obstore.auth.boto3.StsCredentialProvider")
    @patch("boto3.Session")
    def test_no_region_skips_region_kwarg(
        self, mock_session_cls: MagicMock, mock_sts_cls: MagicMock
    ) -> None:
        from application_sdk.storage._credential_providers import (
            make_s3_assume_role_provider,
        )

        mock_session_cls.return_value = MagicMock()
        mock_sts_cls.return_value = MagicMock()

        make_s3_assume_role_provider(role_arn="arn:aws:iam::123:role/R")

        session_kwargs = mock_session_cls.call_args.kwargs
        assert "region_name" not in session_kwargs

    def test_missing_boto3_raises_config_error(self) -> None:
        with patch.dict("sys.modules", {"boto3": None, "obstore.auth.boto3": None}):
            import application_sdk.storage._credential_providers as cp

            with pytest.raises((StorageConfigError, ImportError)):
                cp.make_s3_assume_role_provider(role_arn="arn:aws:iam::123:role/R")


class TestMakeAzureCertificateProvider:
    @patch("obstore.auth.azure.AzureCredentialProvider")
    @patch("azure.identity.CertificateCredential")
    def test_certificate_path_creates_provider(
        self, mock_cert_cred: MagicMock, mock_az_prov: MagicMock
    ) -> None:
        from application_sdk.storage._credential_providers import (
            make_azure_certificate_provider,
        )

        mock_cred_instance = MagicMock()
        mock_cert_cred.return_value = mock_cred_instance
        mock_provider = MagicMock()
        mock_az_prov.return_value = mock_provider

        result = make_azure_certificate_provider(
            tenant_id="tid",
            client_id="cid",
            certificate_path="/certs/app.pfx",
            certificate_password="pass",
        )

        mock_cert_cred.assert_called_once_with(
            tenant_id="tid",
            client_id="cid",
            certificate_path="/certs/app.pfx",
            password="pass",
        )
        mock_az_prov.assert_called_once_with(credential=mock_cred_instance)
        assert result is mock_provider

    @patch("obstore.auth.azure.AzureCredentialProvider")
    @patch("azure.identity.CertificateCredential")
    def test_certificate_data_passed_correctly(
        self, mock_cert_cred: MagicMock, mock_az_prov: MagicMock
    ) -> None:
        from application_sdk.storage._credential_providers import (
            make_azure_certificate_provider,
        )

        mock_cert_cred.return_value = MagicMock()
        mock_az_prov.return_value = MagicMock()

        make_azure_certificate_provider(
            tenant_id="tid",
            client_id="cid",
            certificate_data=b"CERT_BYTES",
        )

        kwargs = mock_cert_cred.call_args.kwargs
        assert kwargs["certificate_data"] == b"CERT_BYTES"
        assert "certificate_path" not in kwargs

    @patch("obstore.auth.azure.AzureCredentialProvider")
    @patch("azure.identity.CertificateCredential")
    def test_authority_host_forwarded(
        self, mock_cert_cred: MagicMock, mock_az_prov: MagicMock
    ) -> None:
        from application_sdk.storage._credential_providers import (
            make_azure_certificate_provider,
        )

        mock_cert_cred.return_value = MagicMock()
        mock_az_prov.return_value = MagicMock()

        make_azure_certificate_provider(
            tenant_id="tid",
            client_id="cid",
            certificate_path="/c.pfx",
            authority_host="https://login.chinacloudapi.cn",
        )

        kwargs = mock_cert_cred.call_args.kwargs
        assert kwargs["authority"] == "https://login.chinacloudapi.cn"

    def test_missing_azure_identity_raises_config_error(self) -> None:
        with patch.dict(
            "sys.modules",
            {"azure.identity": None, "obstore.auth.azure": None},
        ):
            import application_sdk.storage._credential_providers as cp

            with pytest.raises((StorageConfigError, ImportError)):
                cp.make_azure_certificate_provider(
                    tenant_id="tid",
                    client_id="cid",
                    certificate_path="/c.pfx",
                )
