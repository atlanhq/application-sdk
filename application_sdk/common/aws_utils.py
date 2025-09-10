import re
from typing import Any, Dict, Optional

import boto3
from sqlalchemy.engine.url import URL

from application_sdk.constants import AWS_SESSION_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def get_region_name_from_hostname(hostname: str) -> str:
    """
    Extract region name from AWS RDS endpoint.
    Example: database-1.abc123xyz.us-east-1.rds.amazonaws.com -> us-east-1

    Args:
        hostname (str): The RDS host endpoint

    Returns:
        str: AWS region name
    """
    match = re.search(r"\.([a-z]{2}-[a-z]+-\d)\.", hostname)
    if match:
        return match.group(1)
    # Some services may use - instead of . (rare)
    match = re.search(r"-([a-z]{2}-[a-z]+-\d)\.", hostname)
    if match:
        return match.group(1)
    raise ValueError("Could not find valid AWS region from hostname")


def generate_aws_rds_token_with_iam_role(
    role_arn: str,
    host: str,
    user: str,
    external_id: str | None = None,
    session_name: str = AWS_SESSION_NAME,
    port: int = 5432,
    region: str | None = None,
) -> str:
    """
    Get temporary AWS credentials by assuming a role and generate RDS auth token.

    Args:
        role_arn (str): The ARN of the role to assume
        host (str): The RDS host endpoint
        user (str): The database username
        external_id (str, optional): The external ID to use for the session
        session_name (str, optional): Name of the temporary session
        port (int, optional): Database port
        region (str, optional): AWS region name
    Returns:
        str: RDS authentication token
    """
    from botocore.exceptions import ClientError

    try:
        from boto3 import client

        sts_client = client(
            "sts", region_name=region or get_region_name_from_hostname(host)
        )
        assumed_role = sts_client.assume_role(
            RoleArn=role_arn, RoleSessionName=session_name, ExternalId=external_id or ""
        )

        credentials = assumed_role["Credentials"]
        aws_client = create_aws_client(
            service="rds",
            region=region or get_region_name_from_hostname(host),
            temp_credentials=credentials,
        )
        token: str = aws_client.generate_db_auth_token(
            DBHostname=host, Port=port, DBUsername=user
        )
        return token

    except ClientError as e:
        raise Exception(f"Failed to assume role: {str(e)}")


def generate_aws_rds_token_with_iam_user(
    aws_access_key_id: str,
    aws_secret_access_key: str,
    host: str,
    user: str,
    port: int = 5432,
    region: str | None = None,
) -> str:
    """
    Generate RDS auth token using IAM user credentials.

    Args:
        aws_access_key_id (str): AWS access key ID
        aws_secret_access_key (str): AWS secret access key
        host (str): The RDS host endpoint
        user (str): The database username
        port (int, optional): Database port
        region (str, optional): AWS region name
    Returns:
        str: RDS authentication token
    """
    try:
        from boto3 import client

        aws_client = client(
            "rds",
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region or get_region_name_from_hostname(host),
        )
        token = aws_client.generate_db_auth_token(
            DBHostname=host, Port=port, DBUsername=user
        )
        return token
    except Exception as e:
        raise Exception(f"Failed to get user credentials: {str(e)}")


def get_cluster_identifier(aws_client) -> Optional[str]:
    """
    Retrieve the cluster identifier from AWS Redshift clusters.

    Args:
        aws_client: Boto3 Redshift client instance

    Returns:
        str: The cluster identifier

    Raises:
        RuntimeError: If no clusters are found
    """
    clusters = aws_client.describe_clusters()

    for cluster in clusters["Clusters"]:
        cluster_identifier = cluster.get("ClusterIdentifier")
        if cluster_identifier:
            # Optionally, you can add logic to filter clusters if needed
            # we are reading first clusters ID if not provided
            return cluster_identifier  # Just return the string
    return None


def create_aws_session(credentials: Dict[str, Any]) -> boto3.Session:
    """
    Create a boto3 session with AWS credentials.

    Args:
        credentials: Dictionary containing AWS credentials

    Returns:
        boto3.Session: Configured boto3 session
    """
    aws_access_key_id = credentials.get("aws_access_key_id") or credentials.get(
        "username"
    )
    aws_secret_access_key = credentials.get("aws_secret_access_key") or credentials.get(
        "password"
    )

    return boto3.Session(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )


def get_cluster_credentials(
    aws_client, credentials: Dict[str, Any], extra: Dict[str, Any]
) -> Dict[str, str]:
    """
    Retrieve cluster credentials using IAM authentication.

    Args:
        aws_client: Boto3 Redshift client instance
        credentials: Dictionary containing connection credentials

    Returns:
        Dict[str, str]: Dictionary containing DbUser and DbPassword
    """
    database = extra["database"]
    cluster_identifier = credentials.get("cluster_id") or get_cluster_identifier(
        aws_client
    )
    return aws_client.get_cluster_credentials_with_iam(
        DbName=database,
        ClusterIdentifier=cluster_identifier,
    )


def create_aws_client(
    service: str,
    region: str,
    session: Optional[boto3.Session] = None,
    temp_credentials: Optional[Dict[str, str]] = None,
    use_default_credentials: bool = False,
) -> Any:
    """
    Create an AWS client with flexible credential options.

    Args:
        service: AWS service name (e.g., 'redshift', 'redshift-serverless', 'sts', 'rds')
        region: AWS region name
        session: Optional boto3 session instance. If provided, uses session credentials
        temp_credentials: Optional dictionary containing temporary credentials from assume_role.
                         Must contain 'AccessKeyId', 'SecretAccessKey', and 'SessionToken'
        use_default_credentials: If True, uses default AWS credentials (environment, IAM role, etc.)
                                This is the fallback if no other credentials are provided

    Returns:
        AWS client instance

    Raises:
        ValueError: If invalid credential combination is provided
        Exception: If client creation fails

    Examples:
        Using temporary credentials::

            client = create_aws_client(
                service="redshift",
                region="us-east-1",
                temp_credentials={
                    "AccessKeyId": "AKIA...",
                    "SecretAccessKey": "...",
                    "SessionToken": "..."
                }
            )

        Using a session::

            session = boto3.Session(profile_name="my-profile")
            client = create_aws_client(
                service="rds",
                region="us-west-2",
                session=session
            )

        Using default credentials::

            client = create_aws_client(
                service="sts",
                region="us-east-1",
                use_default_credentials=True
            )
    """
    # Validate credential options
    credential_sources = sum(
        [session is not None, temp_credentials is not None, use_default_credentials]
    )

    if credential_sources == 0:
        raise ValueError("At least one credential source must be provided")
    if credential_sources > 1:
        raise ValueError("Only one credential source should be provided at a time")

    try:
        # Priority 1: Use provided session
        if session is not None:
            logger.debug(
                f"Creating {service} client using provided session in region {region}"
            )
            return session.client(service, region_name=region)  # type: ignore

        # Priority 2: Use temporary credentials
        if temp_credentials is not None:
            logger.debug(
                f"Creating {service} client using temporary credentials in region {region}"
            )
            return boto3.client(  # type: ignore
                service,
                aws_access_key_id=temp_credentials["AccessKeyId"],
                aws_secret_access_key=temp_credentials["SecretAccessKey"],
                aws_session_token=temp_credentials["SessionToken"],
                region_name=region,
            )

        # Priority 3: Use default credentials
        if use_default_credentials:
            logger.debug(
                f"Creating {service} client using default credentials in region {region}"
            )
            return boto3.client(service, region_name=region)  # type: ignore

    except Exception as e:
        logger.error(f"Failed to create {service} client in region {region}: {e}")
        raise Exception(f"Failed to create {service} client: {str(e)}")


def create_engine_url(
    drivername: str,
    credentials: Dict[str, Any],
    cluster_credentials: Dict[str, str],
    extra: Dict[str, Any],
) -> URL:
    """
    Create SQLAlchemy engine URL for Redshift connection.

    Args:
        credentials: Dictionary containing connection credentials
        cluster_credentials: Dictionary containing DbUser and DbPassword

    Returns:
        URL: SQLAlchemy engine URL
    """
    host = credentials["host"]
    port = credentials.get("port")
    database = extra["database"]

    return URL.create(
        drivername=drivername,
        username=cluster_credentials["DbUser"],
        password=cluster_credentials["DbPassword"],
        host=host,
        port=port,
        database=database,
    )


def get_all_aws_regions() -> list[str]:
    """
    Get all available AWS regions dynamically using EC2 describe_regions API.
    Returns:
        list[str]: List of all AWS region names
    Raises:
        Exception: If unable to retrieve regions from AWS
    """
    try:
        # Use us-east-1 as the default region for the EC2 client since it's always available
        ec2_client = boto3.client("ec2", region_name="us-east-1")
        response = ec2_client.describe_regions()
        regions = [region["RegionName"] for region in response["Regions"]]
        return sorted(regions)  # Sort for consistent ordering
    except Exception as e:
        # Fallback to a comprehensive hardcoded list if API call fails
        logger.warning(
            f"Failed to retrieve AWS regions dynamically: {e}. Using fallback list."
        )
        return [
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
