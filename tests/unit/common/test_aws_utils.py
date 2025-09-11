from unittest.mock import MagicMock, patch

import pytest

from application_sdk.common.aws_utils import (
    create_aws_client,
    create_aws_session,
    create_engine_url,
    generate_aws_rds_token_with_iam_role,
    generate_aws_rds_token_with_iam_user,
    get_all_aws_regions,
    get_cluster_credentials,
    get_cluster_identifier,
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

    def test_get_region_name_from_hostname_with_dash_separator(self):
        """Test region extraction with dash separator (rare case)."""
        hostname = "db-us-west-2.rds.amazonaws.com"
        result = get_region_name_from_hostname(hostname)
        assert result == "us-west-2"

    # Tests for get_cluster_identifier
    def test_get_cluster_identifier_success(self):
        """Test successful cluster identifier retrieval."""
        mock_client = MagicMock()
        mock_client.describe_clusters.return_value = {
            "Clusters": [
                {"ClusterIdentifier": "test-cluster-1"},
                {"ClusterIdentifier": "test-cluster-2"},
            ]
        }

        result = get_cluster_identifier(mock_client)
        assert result == "test-cluster-1"

    def test_get_cluster_identifier_no_clusters(self):
        """Test cluster identifier retrieval when no clusters exist."""
        mock_client = MagicMock()
        mock_client.describe_clusters.return_value = {"Clusters": []}

        result = get_cluster_identifier(mock_client)
        assert result is None

    def test_get_cluster_identifier_no_identifier(self):
        """Test cluster identifier retrieval when cluster has no identifier."""
        mock_client = MagicMock()
        mock_client.describe_clusters.return_value = {
            "Clusters": [{"ClusterStatus": "available"}]
        }

        result = get_cluster_identifier(mock_client)
        assert result is None

    # Tests for create_aws_session
    def test_create_aws_session_with_aws_credentials(self):
        """Test creating AWS session with aws_access_key_id and aws_secret_access_key."""
        credentials = {
            "aws_access_key_id": "test_key",
            "aws_secret_access_key": "test_secret",
        }

        with patch("boto3.Session") as mock_session:
            create_aws_session(credentials)
            mock_session.assert_called_once_with(
                aws_access_key_id="test_key",
                aws_secret_access_key="test_secret",
            )

    def test_create_aws_session_with_username_password(self):
        """Test creating AWS session with username and password (fallback)."""
        credentials = {
            "username": "test_key",
            "password": "test_secret",
        }

        with patch("boto3.Session") as mock_session:
            create_aws_session(credentials)
            mock_session.assert_called_once_with(
                aws_access_key_id="test_key",
                aws_secret_access_key="test_secret",
            )

    def test_create_aws_session_priority_aws_credentials(self):
        """Test that aws_access_key_id takes priority over username."""
        credentials = {
            "aws_access_key_id": "aws_key",
            "username": "user_key",
            "aws_secret_access_key": "aws_secret",
            "password": "user_secret",
        }

        with patch("boto3.Session") as mock_session:
            create_aws_session(credentials)
            mock_session.assert_called_once_with(
                aws_access_key_id="aws_key",
                aws_secret_access_key="aws_secret",
            )

    # Tests for get_cluster_credentials
    def test_get_cluster_credentials_success(self):
        """Test successful cluster credentials retrieval."""
        mock_client = MagicMock()
        mock_client.get_cluster_credentials_with_iam.return_value = {
            "DbUser": "test_user",
            "DbPassword": "test_password",
        }

        credentials = {"cluster_id": "test-cluster"}
        extra = {"database": "test_db"}

        result = get_cluster_credentials(mock_client, credentials, extra)

        assert result == {"DbUser": "test_user", "DbPassword": "test_password"}
        mock_client.get_cluster_credentials_with_iam.assert_called_once_with(
            DbName="test_db", ClusterIdentifier="test-cluster"
        )

    def test_get_cluster_credentials_without_cluster_id(self):
        """Test cluster credentials retrieval without explicit cluster_id."""
        mock_client = MagicMock()
        mock_client.get_cluster_credentials_with_iam.return_value = {
            "DbUser": "test_user",
            "DbPassword": "test_password",
        }

        # Mock get_cluster_identifier to return a cluster ID
        with patch(
            "application_sdk.common.aws_utils.get_cluster_identifier"
        ) as mock_get_cluster:
            mock_get_cluster.return_value = "auto-detected-cluster"

            credentials = {}
            extra = {"database": "test_db"}

            result = get_cluster_credentials(mock_client, credentials, extra)

            assert result == {"DbUser": "test_user", "DbPassword": "test_password"}
            mock_client.get_cluster_credentials_with_iam.assert_called_once_with(
                DbName="test_db", ClusterIdentifier="auto-detected-cluster"
            )

    # Tests for create_aws_client
    def test_create_aws_client_with_session(self):
        """Test creating AWS client with session."""
        mock_session = MagicMock()
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client

        result = create_aws_client(
            service="redshift", region="us-east-1", session=mock_session
        )

        assert result == mock_client
        mock_session.client.assert_called_once_with("redshift", region_name="us-east-1")

    def test_create_aws_client_with_default_credentials(self):
        """Test creating AWS client with default credentials."""
        with patch("boto3.client") as mock_client:
            mock_client_instance = MagicMock()
            mock_client.return_value = mock_client_instance

            result = create_aws_client(
                service="ec2", region="eu-west-1", use_default_credentials=True
            )

            assert result == mock_client_instance
            mock_client.assert_called_once_with("ec2", region_name="eu-west-1")

    def test_create_aws_client_no_credentials(self):
        """Test creating AWS client with no credentials provided."""
        with pytest.raises(
            ValueError, match="At least one credential source must be provided"
        ):
            create_aws_client(service="s3", region="us-east-1")

    def test_create_aws_client_multiple_credentials(self):
        """Test creating AWS client with multiple credential sources."""
        mock_session = MagicMock()
        temp_credentials = {
            "AccessKeyId": "temp_key",
            "SecretAccessKey": "temp_secret",
            "SessionToken": "temp_token",
        }

        with pytest.raises(
            ValueError, match="Only one credential source should be provided at a time"
        ):
            create_aws_client(
                service="s3",
                region="us-east-1",
                session=mock_session,
                temp_credentials=temp_credentials,
            )

    def test_create_aws_client_exception(self):
        """Test creating AWS client with exception."""
        with patch("boto3.client") as mock_client:
            mock_client.side_effect = Exception("AWS service error")

            with pytest.raises(Exception, match="Failed to create s3 client"):
                create_aws_client(
                    service="s3", region="us-east-1", use_default_credentials=True
                )

    # Tests for create_engine_url
    def test_create_engine_url_success(self):
        """Test successful engine URL creation."""
        credentials = {"host": "test-host", "port": 5432}
        cluster_credentials = {"DbUser": "test_user", "DbPassword": "test_password"}
        extra = {"database": "test_db"}

        result = create_engine_url(
            drivername="postgresql+psycopg2",
            credentials=credentials,
            cluster_credentials=cluster_credentials,
            extra=extra,
        )

        assert result.drivername == "postgresql+psycopg2"
        assert result.username == "test_user"
        assert result.password == "test_password"
        assert result.host == "test-host"
        assert result.port == 5432
        assert result.database == "test_db"

    def test_create_engine_url_without_port(self):
        """Test engine URL creation without port."""
        credentials = {"host": "test-host"}
        cluster_credentials = {"DbUser": "test_user", "DbPassword": "test_password"}
        extra = {"database": "test_db"}

        result = create_engine_url(
            drivername="redshift+psycopg2",
            credentials=credentials,
            cluster_credentials=cluster_credentials,
            extra=extra,
        )

        assert result.drivername == "redshift+psycopg2"
        assert result.username == "test_user"
        assert result.password == "test_password"
        assert result.host == "test-host"
        assert result.port is None
        assert result.database == "test_db"

    # Tests for get_all_aws_regions
    @patch("boto3.client")
    def test_get_all_aws_regions_success(self, mock_client):
        """Test successful retrieval of AWS regions."""
        mock_ec2 = MagicMock()
        mock_client.return_value = mock_ec2
        mock_ec2.describe_regions.return_value = {
            "Regions": [
                {"RegionName": "us-east-1"},
                {"RegionName": "us-west-2"},
                {"RegionName": "eu-west-1"},
                {"RegionName": "ap-southeast-1"},
            ]
        }

        result = get_all_aws_regions()

        assert result == ["ap-southeast-1", "eu-west-1", "us-east-1", "us-west-2"]
        mock_client.assert_called_once_with("ec2", region_name="us-east-1")
        mock_ec2.describe_regions.assert_called_once()

    @patch("boto3.client")
    def test_get_all_aws_regions_fallback(self, mock_client):
        """Test fallback to hardcoded regions when API call fails."""
        mock_ec2 = MagicMock()
        mock_client.return_value = mock_ec2
        mock_ec2.describe_regions.side_effect = Exception("API error")

        result = get_all_aws_regions()

        expected_regions = [
            "ap-northeast-1",
            "ap-south-1",
            "ap-southeast-1",
            "ap-southeast-2",
            "aws-global",
            "ca-central-1",
            "eu-central-1",
            "eu-north-1",
            "eu-west-1",
            "eu-west-2",
            "eu-west-3",
            "sa-east-1",
            "us-east-1",
            "us-east-2",
            "us-west-1",
            "us-west-2",
        ]
        assert result == expected_regions
        mock_client.assert_called_once_with("ec2", region_name="us-east-1")
        mock_ec2.describe_regions.assert_called_once()

    @patch("boto3.client")
    def test_get_all_aws_regions_empty_response(self, mock_client):
        """Test handling of empty regions response."""
        mock_ec2 = MagicMock()
        mock_client.return_value = mock_ec2
        mock_ec2.describe_regions.return_value = {"Regions": []}

        result = get_all_aws_regions()

        assert result == []
        mock_client.assert_called_once_with("ec2", region_name="us-east-1")
        mock_ec2.describe_regions.assert_called_once()
