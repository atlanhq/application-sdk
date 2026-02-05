"""
Test suite for validating the AWS ExternalId fix in aws_utils.

This test validates the fix for the bug where passing an empty string or None
as external_id would cause AWS STS AssumeRole to fail.

Root Cause:
-----------
AWS STS AssumeRole API has specific constraints for ExternalId:
- ExternalId is OPTIONAL (Required: No)
- If provided, it MUST be 2-1224 characters (minimum length of 2)
- Passing ExternalId="" (empty string) fails AWS validation

The original code was:
    ExternalId=external_id or ""  # ❌ Converts None to "" which fails AWS validation

The fix conditionally includes ExternalId only when it's valid:
    if external_id and len(external_id.strip()) >= 2:
        assume_role(..., ExternalId=external_id)
    else:
        assume_role(...)  # No ExternalId parameter

AWS Documentation Reference:
https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html
- ExternalId: Type: String, Length Constraints: Minimum 2, Maximum 1224, Required: No
"""

import pytest
from unittest.mock import patch, MagicMock
from typing import Dict, Any


class TestExternalIdHandling:
    """Test that external_id is handled correctly per AWS STS API constraints."""

    @pytest.fixture
    def mock_sts_client(self):
        """Create a mock STS client."""
        mock_client = MagicMock()
        mock_client.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "ASIAIOSFODNN7EXAMPLE",
                "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
                "SessionToken": "mock-session-token",
            }
        }
        return mock_client

    @pytest.fixture
    def mock_rds_client(self):
        """Create a mock RDS client."""
        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "mock-db-auth-token"
        return mock_client

    def test_valid_external_id_is_passed(self, mock_sts_client, mock_rds_client):
        """
        Test that a valid external_id (2+ characters) is passed to assume_role.
        """
        from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

        with patch("boto3.client") as mock_boto_client, \
             patch("application_sdk.common.aws_utils.create_aws_client") as mock_create_client:
            
            mock_boto_client.return_value = mock_sts_client
            mock_create_client.return_value = mock_rds_client

            generate_aws_rds_token_with_iam_role(
                role_arn="arn:aws:iam::123456789012:role/TestRole",
                host="mydb.abc123.us-east-1.rds.amazonaws.com",
                user="db_user",
                external_id="valid-external-id-12345",  # Valid: 24 characters
            )

            # Verify assume_role was called WITH ExternalId
            mock_sts_client.assume_role.assert_called_once()
            call_kwargs = mock_sts_client.assume_role.call_args[1]
            
            assert "ExternalId" in call_kwargs, (
                "ExternalId should be passed when a valid external_id is provided"
            )
            assert call_kwargs["ExternalId"] == "valid-external-id-12345"

    def test_none_external_id_is_not_passed(self, mock_sts_client, mock_rds_client):
        """
        Test that None external_id does NOT result in ExternalId being passed.
        
        AWS STS will reject ExternalId="" (empty string), so we must omit
        the parameter entirely when external_id is None.
        """
        from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

        with patch("boto3.client") as mock_boto_client, \
             patch("application_sdk.common.aws_utils.create_aws_client") as mock_create_client:
            
            mock_boto_client.return_value = mock_sts_client
            mock_create_client.return_value = mock_rds_client

            generate_aws_rds_token_with_iam_role(
                role_arn="arn:aws:iam::123456789012:role/TestRole",
                host="mydb.abc123.us-east-1.rds.amazonaws.com",
                user="db_user",
                external_id=None,  # None should NOT be passed to AWS
            )

            # Verify assume_role was called WITHOUT ExternalId
            mock_sts_client.assume_role.assert_called_once()
            call_kwargs = mock_sts_client.assume_role.call_args[1]
            
            assert "ExternalId" not in call_kwargs, (
                "ExternalId should NOT be passed when external_id is None. "
                f"Got call kwargs: {call_kwargs}"
            )

    def test_empty_string_external_id_is_not_passed(self, mock_sts_client, mock_rds_client):
        """
        Test that empty string external_id does NOT result in ExternalId being passed.
        
        AWS STS requires ExternalId to be 2-1224 characters if provided.
        An empty string "" would fail AWS validation.
        """
        from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

        with patch("boto3.client") as mock_boto_client, \
             patch("application_sdk.common.aws_utils.create_aws_client") as mock_create_client:
            
            mock_boto_client.return_value = mock_sts_client
            mock_create_client.return_value = mock_rds_client

            generate_aws_rds_token_with_iam_role(
                role_arn="arn:aws:iam::123456789012:role/TestRole",
                host="mydb.abc123.us-east-1.rds.amazonaws.com",
                user="db_user",
                external_id="",  # Empty string should NOT be passed to AWS
            )

            # Verify assume_role was called WITHOUT ExternalId
            mock_sts_client.assume_role.assert_called_once()
            call_kwargs = mock_sts_client.assume_role.call_args[1]
            
            assert "ExternalId" not in call_kwargs, (
                "ExternalId should NOT be passed when external_id is empty string. "
                f"Got call kwargs: {call_kwargs}"
            )

    def test_single_char_external_id_is_not_passed(self, mock_sts_client, mock_rds_client):
        """
        Test that single character external_id does NOT result in ExternalId being passed.
        
        AWS STS requires ExternalId to be at least 2 characters.
        """
        from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

        with patch("boto3.client") as mock_boto_client, \
             patch("application_sdk.common.aws_utils.create_aws_client") as mock_create_client:
            
            mock_boto_client.return_value = mock_sts_client
            mock_create_client.return_value = mock_rds_client

            generate_aws_rds_token_with_iam_role(
                role_arn="arn:aws:iam::123456789012:role/TestRole",
                host="mydb.abc123.us-east-1.rds.amazonaws.com",
                user="db_user",
                external_id="x",  # Single char - too short for AWS
            )

            # Verify assume_role was called WITHOUT ExternalId
            mock_sts_client.assume_role.assert_called_once()
            call_kwargs = mock_sts_client.assume_role.call_args[1]
            
            assert "ExternalId" not in call_kwargs, (
                "ExternalId should NOT be passed when external_id is only 1 character. "
                f"Got call kwargs: {call_kwargs}"
            )

    def test_whitespace_only_external_id_is_not_passed(self, mock_sts_client, mock_rds_client):
        """
        Test that whitespace-only external_id does NOT result in ExternalId being passed.
        
        After stripping whitespace, the string would be empty/too short.
        """
        from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

        with patch("boto3.client") as mock_boto_client, \
             patch("application_sdk.common.aws_utils.create_aws_client") as mock_create_client:
            
            mock_boto_client.return_value = mock_sts_client
            mock_create_client.return_value = mock_rds_client

            generate_aws_rds_token_with_iam_role(
                role_arn="arn:aws:iam::123456789012:role/TestRole",
                host="mydb.abc123.us-east-1.rds.amazonaws.com",
                user="db_user",
                external_id="   ",  # Whitespace only - should be treated as invalid
            )

            # Verify assume_role was called WITHOUT ExternalId
            mock_sts_client.assume_role.assert_called_once()
            call_kwargs = mock_sts_client.assume_role.call_args[1]
            
            assert "ExternalId" not in call_kwargs, (
                "ExternalId should NOT be passed when external_id is whitespace only. "
                f"Got call kwargs: {call_kwargs}"
            )

    def test_minimum_valid_external_id(self, mock_sts_client, mock_rds_client):
        """
        Test that the minimum valid external_id (2 characters) IS passed.
        """
        from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

        with patch("boto3.client") as mock_boto_client, \
             patch("application_sdk.common.aws_utils.create_aws_client") as mock_create_client:
            
            mock_boto_client.return_value = mock_sts_client
            mock_create_client.return_value = mock_rds_client

            generate_aws_rds_token_with_iam_role(
                role_arn="arn:aws:iam::123456789012:role/TestRole",
                host="mydb.abc123.us-east-1.rds.amazonaws.com",
                user="db_user",
                external_id="ab",  # Minimum valid: exactly 2 characters
            )

            # Verify assume_role was called WITH ExternalId
            mock_sts_client.assume_role.assert_called_once()
            call_kwargs = mock_sts_client.assume_role.call_args[1]
            
            assert "ExternalId" in call_kwargs, (
                "ExternalId should be passed when external_id is exactly 2 characters"
            )
            assert call_kwargs["ExternalId"] == "ab"

    def test_default_parameter_omits_external_id(self, mock_sts_client, mock_rds_client):
        """
        Test that omitting external_id parameter entirely (default None) works correctly.
        """
        from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

        with patch("boto3.client") as mock_boto_client, \
             patch("application_sdk.common.aws_utils.create_aws_client") as mock_create_client:
            
            mock_boto_client.return_value = mock_sts_client
            mock_create_client.return_value = mock_rds_client

            # Call WITHOUT specifying external_id at all
            generate_aws_rds_token_with_iam_role(
                role_arn="arn:aws:iam::123456789012:role/TestRole",
                host="mydb.abc123.us-east-1.rds.amazonaws.com",
                user="db_user",
                # external_id not provided - should default to None
            )

            # Verify assume_role was called WITHOUT ExternalId
            mock_sts_client.assume_role.assert_called_once()
            call_kwargs = mock_sts_client.assume_role.call_args[1]
            
            assert "ExternalId" not in call_kwargs, (
                "ExternalId should NOT be passed when external_id parameter is omitted. "
                f"Got call kwargs: {call_kwargs}"
            )


class TestExternalIdFromPostgresConfig:
    """
    Test external_id handling matches postgres.yaml configuration.
    
    Per postgres.yaml (lines 665-741), IAM Role auth has:
    - extra.aws_role_arn: Required
    - extra.aws_external_id: Optional (may not be provided)
    """

    @pytest.fixture
    def mock_sts_client(self):
        """Create a mock STS client."""
        mock_client = MagicMock()
        mock_client.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "ASIAIOSFODNN7EXAMPLE",
                "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
                "SessionToken": "mock-session-token",
            }
        }
        return mock_client

    @pytest.fixture
    def mock_rds_client(self):
        """Create a mock RDS client."""
        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "mock-db-auth-token"
        return mock_client

    def test_iam_role_with_external_id(self, mock_sts_client, mock_rds_client):
        """Test IAM role auth when external_id IS provided in config."""
        from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

        # Simulating credentials from postgres.yaml IAM Role config
        iam_role_credentials = {
            "authType": "iam_role",
            "host": "mydb.abc123.us-east-1.rds.amazonaws.com",
            "port": 5432,
            "username": "db_user",
            "extra": {
                "aws_role_arn": "arn:aws:iam::123456789012:role/RDSRole",
                "aws_external_id": "12345678-1234-1234-1234",  # User provided
                "database": "mydb",
            },
        }

        with patch("boto3.client") as mock_boto_client, \
             patch("application_sdk.common.aws_utils.create_aws_client") as mock_create_client:
            
            mock_boto_client.return_value = mock_sts_client
            mock_create_client.return_value = mock_rds_client

            generate_aws_rds_token_with_iam_role(
                role_arn=iam_role_credentials["extra"]["aws_role_arn"],
                host=iam_role_credentials["host"],
                user=iam_role_credentials["username"],
                external_id=iam_role_credentials["extra"]["aws_external_id"],
            )

            call_kwargs = mock_sts_client.assume_role.call_args[1]
            assert "ExternalId" in call_kwargs
            assert call_kwargs["ExternalId"] == "12345678-1234-1234-1234"

    def test_iam_role_without_external_id(self, mock_sts_client, mock_rds_client):
        """Test IAM role auth when external_id is NOT provided in config."""
        from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

        # Simulating credentials from postgres.yaml IAM Role config WITHOUT external_id
        iam_role_credentials = {
            "authType": "iam_role",
            "host": "mydb.abc123.us-east-1.rds.amazonaws.com",
            "port": 5432,
            "username": "db_user",
            "extra": {
                "aws_role_arn": "arn:aws:iam::123456789012:role/RDSRole",
                # aws_external_id NOT provided
                "database": "mydb",
            },
        }

        with patch("boto3.client") as mock_boto_client, \
             patch("application_sdk.common.aws_utils.create_aws_client") as mock_create_client:
            
            mock_boto_client.return_value = mock_sts_client
            mock_create_client.return_value = mock_rds_client

            generate_aws_rds_token_with_iam_role(
                role_arn=iam_role_credentials["extra"]["aws_role_arn"],
                host=iam_role_credentials["host"],
                user=iam_role_credentials["username"],
                external_id=iam_role_credentials["extra"].get("aws_external_id"),  # Returns None
            )

            call_kwargs = mock_sts_client.assume_role.call_args[1]
            assert "ExternalId" not in call_kwargs, (
                "ExternalId should NOT be passed when not provided in config"
            )
