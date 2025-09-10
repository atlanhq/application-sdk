import re
import socket
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

import boto3
import sqlalchemy
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
        aws_client = client(
            "rds",
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=region or get_region_name_from_hostname(host),
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
    Retrieve the cluster identifier from AWS clusters.

    Args:
        aws_client: Boto3 client instance

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
        aws_client: Boto3 client instance
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


def create_aws_client(session: boto3.Session, region: str, service: str) -> Any:
    """
    Create an AWS client using the provided session and region.

    Args:
        session: Boto3 session instance
        region: AWS region name
        service: AWS service name (e.g., 'redshift', 'rds', 'sts')

    Returns:
        AWS client instance
    """
    # Use type: ignore to suppress the overload mismatch warning
    # The boto3 client method has many overloads for different services
    # but we need to support dynamic service names
    return session.client(service, region_name=region)  # type: ignore


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


def create_boto3_client(
    service_name: str,
    region_name: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_session_token: str | None = None,
    **kwargs,
) -> Any:
    """
    Create a boto3 client with flexible credential and region configuration.

    Args:
        service_name: AWS service name (e.g., 'sts', 'redshift', 'redshift-serverless', 'rds')
        region_name: AWS region name
        aws_access_key_id: AWS access key ID (optional, for temporary credentials)
        aws_secret_access_key: AWS secret access key (optional, for temporary credentials)
        aws_session_token: AWS session token (optional, for temporary credentials)
        **kwargs: Additional parameters to pass to boto3.client()

    Returns:
        AWS client instance

    Examples:
        Basic client with default credentials::

            sts_client = create_boto3_client("sts", region_name="us-east-1")

        Client with temporary credentials::

            redshift_client = create_boto3_client(
                "redshift",
                region_name="us-east-1",
                aws_access_key_id="AKIA...",
                aws_secret_access_key="...",
                aws_session_token="..."
            )
    """
    client_kwargs = {"region_name": region_name} if region_name else {}

    # Add temporary credentials if provided
    if aws_access_key_id:
        client_kwargs["aws_access_key_id"] = aws_access_key_id
    if aws_secret_access_key:
        client_kwargs["aws_secret_access_key"] = aws_secret_access_key
    if aws_session_token:
        client_kwargs["aws_session_token"] = aws_session_token

    # Add any additional kwargs
    client_kwargs.update(kwargs)

    # Use type: ignore to suppress the overload mismatch warning
    # The boto3 client method has many overloads for different services
    # but we need to support dynamic service names
    return boto3.client(service_name, **client_kwargs)  # type: ignore


def setup_aws_role_based_authentication(
    credentials: Dict[str, Any],
    extra: Dict[str, Any],
    drivername:str,
    session_name: str,
    duration_seconds: int = 3600,
) -> tuple[Any, Any]:
    """
    Set up database connection using AWS role-based authentication.

    This method handles the complete flow of:
    1. Assuming an IAM role across multiple regions
    2. Getting temporary credentials
    3. Retrieving database cluster credentials (Redshift/Redshift Serverless)
    4. Creating SQLAlchemy connection

    Args:
        credentials: Dictionary containing connection credentials
        extra: Dictionary containing additional connection parameters
        drivername: SQLAlchemy driver name (default: "redshift+psycopg2")
        session_name: Name for the assumed role session
        duration_seconds: Duration for the assumed role session

    Returns:
        tuple: (engine, connection) SQLAlchemy engine and connection objects

    Raises:
        ValueError: If required parameters are missing or role assumption fails
        Exception: If connection setup fails
    """
    # Extract configuration
    cluster_id = extra.get("cluster_id", None)
    deployment_type = extra.get("deployment_type", None)
    workgroup = extra.get("workgroup", None)
    database = extra["database"]
    db_user = extra["dbuser"]  # Database user (not IAM user)
    region = extra.get("region_name", None)
    host = credentials["host"]
    port = credentials.get("port", 5439)
    role_arn = extra["aws_role_arn"]
    external_id = extra.get("aws_external_id")  # External ID may be optional

    # Determine region
    if region is None or region == "":
        region = get_region_name_from_hostname(credentials["host"])

    if region is None or region == "":
        logger.info(
            "Private link enabled host detected, switching to finding the region"
        )
        filtered_hostname = socket.getfqdn(credentials["host"])
        extracted_region = get_region_name_from_hostname(filtered_hostname)
        logger.info(
            f"Region extracted from private link enabled host: {extracted_region} and filtered_hostname is {filtered_hostname}"
        )
        region = extracted_region

    # Get all AWS regions and prioritize the detected region
    aws_regions = get_all_aws_regions()
    if region and region in aws_regions:
        aws_regions.remove(region)
        aws_regions.insert(0, region)

    # Step 1: Assume the IAM Role
    assumed_role = None
    for region_arn in aws_regions:
        try:
            logger.info(f"Assuming role in region {region_arn}")
            sts_client = create_boto3_client("sts", region_name=region_arn)

            assume_role_kwargs = {
                "RoleArn": role_arn,
                "RoleSessionName": session_name,
                "DurationSeconds": duration_seconds,
            }

            if external_id:
                assume_role_kwargs["ExternalId"] = external_id

            assumed_role = sts_client.assume_role(**assume_role_kwargs)
            logger.info(f"Successfully assumed role in region {region_arn}")
            break
        except Exception as e:
            logger.info(
                f"Error assuming role in region {region_arn}. Trying with other regions: {e}"
            )
            continue

    if assumed_role is None:
        raise ValueError("Failed to assume role in any region")

    temp_credentials = assumed_role["Credentials"]

    # Step 2: Use the assumed credentials to get database cluster credentials
    redshift = create_boto3_client(
        "redshift",
        region_name=region,
        aws_access_key_id=temp_credentials["AccessKeyId"],
        aws_secret_access_key=temp_credentials["SecretAccessKey"],
        aws_session_token=temp_credentials["SessionToken"],
    )

    # Get cluster credentials based on deployment type
    if deployment_type == "serverless" and workgroup:
        logger.info(f"Workgroup is provided, using workgroup: {workgroup}")
        redshift_serverless = create_boto3_client(
            "redshift-serverless",
            region_name=region,
            aws_access_key_id=temp_credentials["AccessKeyId"],
            aws_secret_access_key=temp_credentials["SecretAccessKey"],
            aws_session_token=temp_credentials["SessionToken"],
        )
        creds = redshift_serverless.get_credentials(
            workgroupName=workgroup,
            dbName=database,
            durationSeconds=duration_seconds,
        )
    elif cluster_id is not None and cluster_id != "":
        logger.info(
            f"Cluster_id is provided, getting cluster_credentials for cluster_id: {cluster_id}"
        )
        creds = redshift.get_cluster_credentials(
            DbUser=db_user,
            DbName=database,
            ClusterIdentifier=cluster_id,
            AutoCreate=False,
        )
    elif cluster_id is None or cluster_id == "":
        logger.info("Cluster_id is not provided, getting cluster_id from redshift")
        cluster_id = get_cluster_credentials(redshift, credentials, extra)
        logger.info(f"Cluster_id is {cluster_id}")

        if cluster_id is None or cluster_id == "":
            raise ValueError("cluster_id or workgroup is required")
        else:
            creds = redshift.get_cluster_credentials(
                DbUser=db_user,
                DbName=database,
                ClusterIdentifier=cluster_id,
                AutoCreate=False,
            )
    else:
        raise ValueError("cluster_id or workgroup is required")

    # Step 3: Build the SQLAlchemy connection string
    if "DbUser" in creds:
        username = quote_plus(creds["DbUser"])
    elif "dbUser" in creds:
        username = quote_plus(creds["dbUser"])
    else:
        raise ValueError("DbUser not found in creds")

    if "DbPassword" in creds:
        password = creds["DbPassword"]
    elif "dbPassword" in creds:
        password = creds["dbPassword"]
    else:
        raise ValueError("DbPassword not found in creds")

    conn_str = f"{drivername}://{username}:{password}@{host}:{port}/{database}"

    # Step 4: Connect using SQLAlchemy
    engine = sqlalchemy.create_engine(
        conn_str, connect_args={"sslmode": "prefer", "connect_timeout": 5}
    )
    connection = engine.connect()

    return engine, connection


def setup_iam_connection(
    credentials: Dict[str, Any],
    extra: Dict[str, Any],
    drivername: str,
    service_name: str,
) -> tuple[Any, Any]:
    """
    Set up database connection using IAM authentication.

    Args:
        credentials: Dictionary containing connection credentials
        extra: Dictionary containing additional connection parameters
        drivername: SQLAlchemy driver name (default: "redshift+psycopg2")

    Returns:
        tuple: (engine, connection) SQLAlchemy engine and connection objects

    Raises:
        ValueError: If region cannot be extracted from host
        Exception: If connection setup fails
    """
    # Create AWS session
    session = create_aws_session(credentials)

    # Extract region from host
    host = credentials["host"]
    region = get_region_name_from_hostname(host)
    if not region:
        raise ValueError(f"Could not extract region from host: {host}")

    # Create Redshift client
    aws_client = create_aws_client(session, region, service_name)

    # Get cluster credentials
    cluster_credentials = get_cluster_credentials(aws_client, credentials, extra)

    # Create engine URL and establish connection
    engine_url = create_engine_url(drivername, credentials, cluster_credentials, extra)

    engine = sqlalchemy.create_engine(
        str(engine_url), connect_args={"sslmode": "prefer", "connect_timeout": 5}
    )
    connection = engine.connect()

    return engine, connection


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
