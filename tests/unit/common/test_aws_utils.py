from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from application_sdk.common.aws_utils import (
    generate_aws_rds_token_with_iam_role,
    generate_aws_rds_token_with_iam_user,
    get_region_name_from_hostname,
)


class TestGetRegionNameFromHostname:
    def test_valid_us_region(self) -> None:
        """Test extracting US region from hostname"""
        hostname = "database-1.abc123xyz.us-east-1.rds.amazonaws.com"
        region = get_region_name_from_hostname(hostname)
        assert region == "us-east-1"

    def test_valid_eu_region(self) -> None:
        """Test extracting EU region from hostname"""
        hostname = "database-2.abc123xyz.eu-west-1.rds.amazonaws.com"
        region = get_region_name_from_hostname(hostname)
        assert region == "eu-west-1"

    def test_valid_ap_region(self) -> None:
        """Test extracting Asia Pacific region from hostname"""
        hostname = "database-3.abc123xyz.ap-southeast-1.rds.amazonaws.com"
        region = get_region_name_from_hostname(hostname)
        assert region == "ap-southeast-1"

    def test_invalid_hostname(self) -> None:
        """Test handling invalid hostname without region"""
        hostname = "invalid-hostname.amazonaws.com"
        with pytest.raises(ValueError) as exc_info:
            get_region_name_from_hostname(hostname)
        assert "Could not find valid AWS region in hostname" in str(exc_info.value)

    def test_other_valid_regions(self) -> None:
        """Test extracting other valid AWS regions"""
        test_cases = [
            ("db.xyz.ca-central-1.rds.amazonaws.com", "ca-central-1"),
            ("db.xyz.me-south-1.rds.amazonaws.com", "me-south-1"),
            ("db.xyz.sa-east-1.rds.amazonaws.com", "sa-east-1"),
            ("db.xyz.af-south-1.rds.amazonaws.com", "af-south-1"),
        ]
        for hostname, expected_region in test_cases:
            assert get_region_name_from_hostname(hostname) == expected_region


class TestGenerateAwsRdsTokenWithIamRole:
    @patch("application_sdk.common.aws_utils.client")
    def test_successful_token_generation(self, mock_client: Mock) -> None:
        """Test successful RDS token generation with IAM role"""
        # Mock STS client and its assume_role method
        mock_sts = Mock()
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "test-access-key",
                "SecretAccessKey": "test-secret-key",
                "SessionToken": "test-session-token",
            }
        }

        # Mock RDS client and its generate_db_auth_token method
        mock_rds = Mock()
        mock_rds.generate_db_auth_token.return_value = "test-auth-token"

        # Configure the client mock to return our mock clients
        mock_client.side_effect = [mock_sts, mock_rds]

        result = generate_aws_rds_token_with_iam_role(
            role_arn="test-role-arn",
            host="database.us-east-1.rds.amazonaws.com",
            user="test-user",
            external_id="test-external-id",
            session_name="test-session",
            port=5432,
        )

        assert result == "test-auth-token"
        mock_sts.assume_role.assert_called_once_with(
            RoleArn="test-role-arn",
            RoleSessionName="test-session",
            ExternalId="test-external-id",
        )
        mock_rds.generate_db_auth_token.assert_called_once_with(
            DBHostname="database.us-east-1.rds.amazonaws.com",
            Port=5432,
            DBUsername="test-user",
        )

    @patch("application_sdk.common.aws_utils.client")
    def test_role_assumption_failure(self, mock_client: Mock) -> None:
        """Test handling of role assumption failure"""
        mock_sts = Mock()
        mock_sts.assume_role.side_effect = ClientError(
            {"Error": {"Code": "InvalidRole", "Message": "Role not found"}},
            "AssumeRole",
        )
        mock_client.return_value = mock_sts

        with pytest.raises(Exception) as exc_info:
            generate_aws_rds_token_with_iam_role(
                role_arn="invalid-role",
                host="database.us-east-1.rds.amazonaws.com",
                user="test-user",
            )
        assert "Failed to assume role" in str(exc_info.value)

    @patch("application_sdk.common.aws_utils.client")
    def test_with_explicit_region(self, mock_client: Mock) -> None:
        """Test token generation with explicitly provided region"""
        # Mock STS client and its assume_role method
        mock_sts = Mock()
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "test-access-key",
                "SecretAccessKey": "test-secret-key",
                "SessionToken": "test-session-token",
            }
        }

        # Mock RDS client and its generate_db_auth_token method
        mock_rds = Mock()
        mock_rds.generate_db_auth_token.return_value = "test-auth-token"

        # Configure the client mock to return our mock clients
        mock_client.side_effect = [mock_sts, mock_rds]

        result = generate_aws_rds_token_with_iam_role(
            role_arn="test-role-arn",
            host="database.rds.amazonaws.com",  # Hostname without region
            user="test-user",
            region="us-west-2",  # Explicit region
        )

        assert result == "test-auth-token"
        # Verify both clients were created with the explicit region
        mock_client.assert_any_call("sts", region_name="us-west-2")
        mock_client.assert_any_call(
            "rds",
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
            aws_session_token="test-session-token",
            region_name="us-west-2",
        )


class TestGenerateAwsRdsTokenWithIamUser:
    @patch("application_sdk.common.aws_utils.client")
    def test_successful_token_generation(self, mock_client: Mock) -> None:
        """Test successful RDS token generation with IAM user credentials"""
        mock_rds = Mock()
        mock_rds.generate_db_auth_token.return_value = "test-auth-token"
        mock_client.return_value = mock_rds

        result = generate_aws_rds_token_with_iam_user(
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
            host="database.us-east-1.rds.amazonaws.com",
            user="test-user",
            port=5432,
        )

        assert result == "test-auth-token"
        mock_client.assert_called_once_with(
            "rds",
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
            region_name="us-east-1",
        )
        mock_rds.generate_db_auth_token.assert_called_once_with(
            DBHostname="database.us-east-1.rds.amazonaws.com",
            Port=5432,
            DBUsername="test-user",
        )

    @patch("application_sdk.common.aws_utils.client")
    def test_client_error(self, mock_client: Mock) -> None:
        """Test handling of client error"""
        mock_rds = Mock()
        mock_rds.generate_db_auth_token.side_effect = Exception("Invalid credentials")
        mock_client.return_value = mock_rds

        with pytest.raises(Exception) as exc_info:
            generate_aws_rds_token_with_iam_user(
                aws_access_key_id="invalid-key",
                aws_secret_access_key="invalid-secret",
                host="database.us-east-1.rds.amazonaws.com",
                user="test-user",
            )
        assert "Failed to get user credentials" in str(exc_info.value)

    @patch("application_sdk.common.aws_utils.client")
    def test_with_explicit_region(self, mock_client: Mock) -> None:
        """Test token generation with explicitly provided region"""
        mock_rds = Mock()
        mock_rds.generate_db_auth_token.return_value = "test-auth-token"
        mock_client.return_value = mock_rds

        result = generate_aws_rds_token_with_iam_user(
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
            host="database.rds.amazonaws.com",  # Hostname without region
            user="test-user",
            region="us-west-2",  # Explicit region
        )

        assert result == "test-auth-token"
        mock_client.assert_called_once_with(
            "rds",
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
            region_name="us-west-2",
        )
