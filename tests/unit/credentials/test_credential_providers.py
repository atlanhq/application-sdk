from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from application_sdk.credentials.base import CredentialError
from application_sdk.credentials.factory import CredentialProviderFactory
from application_sdk.credentials.providers.aws_secrets import (
    AWSSecretsManagerCredentialProvider,
)
from application_sdk.credentials.providers.direct import DirectCredentialProvider

# Helper strategy for credentials dictionaries
credential_dict_strategy = st.dictionaries(
    keys=st.text(min_size=1),
    values=st.one_of(st.text(), st.integers(), st.booleans()),
    min_size=1,
)

# Strategy for AWS credentials
aws_credential_strategy = st.fixed_dictionaries(
    {
        "username": st.text(min_size=1),
        "password": st.text(min_size=1),
        "extra": st.fixed_dictionaries(
            {
                "aws_secret_arn": st.text(min_size=1),
                "aws_secret_region": st.text(min_size=1),
                "secret_store": st.just("aws-secrets"),
            }
        ),
    }
)


# Tests for the CredentialProviderFactory
class TestCredentialProviderFactory:
    def test_register_provider(self):
        # Create a mock provider class that implements CredentialProvider
        class MockProvider:
            async def get_credentials(
                self, source_credentials: Dict[str, Any]
            ) -> Dict[str, Any]:
                return source_credentials

        # Register the provider
        CredentialProviderFactory.register_provider("mock", MockProvider)

        # Get the provider and check it's the right type
        provider = CredentialProviderFactory.get_provider("mock")
        assert isinstance(provider, MockProvider)

    def test_get_provider_unsupported(self):
        with pytest.raises(CredentialError):
            CredentialProviderFactory.get_provider("unsupported_provider")


# Tests for the DirectCredentialProvider
class TestDirectCredentialProvider:
    @given(credentials=credential_dict_strategy)
    @pytest.mark.asyncio
    async def test_get_credentials(self, credentials):
        provider = DirectCredentialProvider()
        result = await provider.get_credentials(credentials)
        assert result == credentials


# Tests for the AWSSecretsManagerCredentialProvider
class TestAWSSecretsManagerCredentialProvider:
    @given(credentials=aws_credential_strategy)
    @pytest.mark.asyncio
    async def test_validate_params(self, credentials):
        provider = AWSSecretsManagerCredentialProvider()
        params = provider._validate_params(credentials)

        assert params["aws_secret_arn"] == credentials["extra"]["aws_secret_arn"]
        assert params["aws_secret_region"] == credentials["extra"]["aws_secret_region"]
        assert params["secret_store"] == credentials["extra"]["secret_store"]

    @pytest.mark.asyncio
    async def test_validate_params_missing_arn(self):
        provider = AWSSecretsManagerCredentialProvider()
        credentials = {
            "username": "test_user",
            "password": "test_pass",
            "extra": {"aws_secret_region": "us-west-2"},
        }

        with pytest.raises(CredentialError, match="aws_secret_arn is required"):
            provider._validate_params(credentials)

    @pytest.mark.asyncio
    async def test_validate_params_missing_region(self):
        provider = AWSSecretsManagerCredentialProvider()
        credentials = {
            "username": "test_user",
            "password": "test_pass",
            "extra": {"aws_secret_arn": "arn:aws:secretsmanager:test"},
        }

        with pytest.raises(CredentialError, match="aws_secret_region is required"):
            provider._validate_params(credentials)

    @pytest.mark.asyncio
    async def test_fetch_secret(self):
        with patch("dapr.clients.DaprClient") as mock_dapr_client:
            mock_client = MagicMock()
            mock_dapr_client.return_value.__enter__.return_value = mock_client

            # Mock the get_secret method
            mock_secret = MagicMock()
            mock_secret.secret = {
                "username": "real_username",
                "password": "real_password",
            }
            mock_client.get_secret.return_value = mock_secret

            provider = AWSSecretsManagerCredentialProvider()
            result = await provider._fetch_secret(
                "arn:aws:secretsmanager:test", "us-west-2", "aws-secrets"
            )

            assert result == {"username": "real_username", "password": "real_password"}
            # Updated assertion to match the real implementation
            mock_client.get_secret.assert_called_once_with(
                store_name="aws-secrets-us-west-2", key="arn:aws:secretsmanager:test"
            )

    @pytest.mark.asyncio
    async def test_get_credentials_success(self):
        with patch.object(
            AWSSecretsManagerCredentialProvider, "_validate_params"
        ) as mock_validate, patch.object(
            AWSSecretsManagerCredentialProvider, "_fetch_secret"
        ) as mock_fetch:
            # Set up the mocks
            mock_validate.return_value = {
                "aws_secret_arn": "arn:aws:secretsmanager:test",
                "aws_secret_region": "us-west-2",
                "secret_store": "aws-secrets",
            }
            mock_fetch.return_value = {
                "DB_USERNAME": "real_username",
                "DB_PASSWORD": "real_password",
            }

            provider = AWSSecretsManagerCredentialProvider()
            credentials = {
                "username": "DB_USERNAME",
                "password": "DB_PASSWORD",
                "extra": {
                    "aws_secret_arn": "arn:aws:secretsmanager:test",
                    "aws_secret_region": "us-west-2",
                },
            }

            result = await provider.get_credentials(credentials)

            assert result["username"] == "real_username"
            assert result["password"] == "real_password"
            mock_validate.assert_called_once_with(credentials)
            mock_fetch.assert_called_once_with(
                "arn:aws:secretsmanager:test", "us-west-2", "aws-secrets"
            )

    @pytest.mark.asyncio
    async def test_get_credentials_error(self):
        with patch.object(
            AWSSecretsManagerCredentialProvider, "_validate_params"
        ) as mock_validate:
            # Make the validation raise an error
            mock_validate.side_effect = CredentialError("Test error")

            provider = AWSSecretsManagerCredentialProvider()
            credentials = {
                "username": "test_user",
                "password": "test_pass",
                "extra": {},
            }

            with pytest.raises(
                CredentialError,
                match="Failed to fetch credentials from AWS Secrets Manager: Test error",
            ):
                await provider.get_credentials(credentials)
