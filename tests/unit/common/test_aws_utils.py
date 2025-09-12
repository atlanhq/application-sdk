from unittest.mock import MagicMock, patch

import pytest

from application_sdk.common.aws_utils import (
    generate_aws_rds_token_with_iam_role,
    generate_aws_rds_token_with_iam_user,
    get_region_name_from_hostname,
)


class TestAWSUtils:
    """Test suite for AWS utility functions."""

    def test_get_region_name_from_hostname_valid(self):
        """Test extracting region from valid hostname."""
        hostname = "database-1.abc123xyz.us-east-1.rds.amazonaws.com"
        result = get_region_name_from_hostname(hostname)
        assert result == "us-east-1"

    def test_get_region_name_from_hostname_invalid(self):
        """Test extracting region from invalid hostname."""
        hostname = "invalid.hostname.com"
        with pytest.raises(ValueError, match="Could not find valid AWS region"):
            get_region_name_from_hostname(hostname)

    @patch("boto3.client")
    def test_generate_aws_rds_token_with_iam_role_success(self, mock_client):
        """Test successful RDS token generation with IAM role."""
        mock_sts = MagicMock()
        mock_rds = MagicMock()
        mock_client.side_effect = [mock_sts, mock_rds]

        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "test_key",
                "SecretAccessKey": "test_secret",
                "SessionToken": "test_token",
            }
        }
        mock_rds.generate_db_auth_token.return_value = "test_token"

        result = generate_aws_rds_token_with_iam_role(
            role_arn="arn:aws:iam::123456789012:role/test-role",
            host="database-1.abc123xyz.us-east-1.rds.amazonaws.com",
            user="test_user",
        )

        assert result == "test_token"

    @patch("boto3.client")
    def test_generate_aws_rds_token_with_iam_user_success(self, mock_client):
        """Test successful RDS token generation with IAM user."""
        mock_rds = MagicMock()
        mock_client.return_value = mock_rds
        mock_rds.generate_db_auth_token.return_value = "test_token"

        result = generate_aws_rds_token_with_iam_user(
            aws_access_key_id="test_key",
            aws_secret_access_key="test_secret",
            host="database-1.abc123xyz.us-east-1.rds.amazonaws.com",
            user="test_user",
        )

        assert result == "test_token"

    @patch("boto3.client")
    def test_generate_aws_rds_token_with_iam_role_with_explicit_region(
        self, mock_client
    ):
        """Test RDS token generation with IAM role using explicit region."""
        mock_sts = MagicMock()
        mock_rds = MagicMock()
        mock_client.side_effect = [mock_sts, mock_rds]

        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "test_key",
                "SecretAccessKey": "test_secret",
                "SessionToken": "test_token",
            }
        }
        mock_rds.generate_db_auth_token.return_value = "test_token"

        result = generate_aws_rds_token_with_iam_role(
            role_arn="arn:aws:iam::123456789012:role/test-role",
            host="test.host.com",
            user="test_user",
            region="us-west-2",
        )

        assert result == "test_token"

    @patch("boto3.client")
    def test_generate_aws_rds_token_with_iam_user_with_explicit_region(
        self, mock_client
    ):
        """Test RDS token generation with IAM user using explicit region."""
        mock_rds = MagicMock()
        mock_client.return_value = mock_rds
        mock_rds.generate_db_auth_token.return_value = "test_token"

        result = generate_aws_rds_token_with_iam_user(
            aws_access_key_id="test_key",
            aws_secret_access_key="test_secret",
            host="test.host.com",
            user="test_user",
            region="us-west-2",
        )

        assert result == "test_token"

    @patch("boto3.client")
    def test_generate_aws_rds_token_with_iam_role_error(self, mock_client):
        """Test error handling in RDS token generation with IAM role."""
        from botocore.exceptions import ClientError

        mock_client.side_effect = ClientError(
            error_response={
                "Error": {"Code": "AccessDenied", "Message": "Access denied"}
            },
            operation_name="AssumeRole",
        )

        with pytest.raises(Exception, match="Failed to assume role"):
            generate_aws_rds_token_with_iam_role(
                role_arn="arn:aws:iam::123456789012:role/test-role",
                host="database-1.abc123xyz.us-east-1.rds.amazonaws.com",
                user="test_user",
            )

    @patch("boto3.client")
    def test_generate_aws_rds_token_with_iam_user_error(self, mock_client):
        """Test error handling in RDS token generation with IAM user."""
        mock_client.side_effect = Exception("AWS service error")

        with pytest.raises(Exception, match="Failed to get user credentials"):
            generate_aws_rds_token_with_iam_user(
                aws_access_key_id="test_key",
                aws_secret_access_key="test_secret",
                host="database-1.abc123xyz.us-east-1.rds.amazonaws.com",
                user="test_user",
            )

    def test_get_region_name_from_hostname_various_regions(self):
        """Test region extraction from various AWS regions."""
        test_cases = [
            ("db.us-west-2.rds.amazonaws.com", "us-west-2"),
            ("db.eu-west-1.rds.amazonaws.com", "eu-west-1"),
            ("db.ap-southeast-1.rds.amazonaws.com", "ap-southeast-1"),
            ("db.ca-central-1.rds.amazonaws.com", "ca-central-1"),
            ("db.me-south-1.rds.amazonaws.com", "me-south-1"),
            ("db.sa-east-1.rds.amazonaws.com", "sa-east-1"),
            ("db.af-south-1.rds.amazonaws.com", "af-south-1"),
        ]

        for hostname, expected_region in test_cases:
            result = get_region_name_from_hostname(hostname)
            assert result == expected_region
