from application_sdk.constants import AWS_SESSION_NAME


def get_region_name_from_hostname(hostname: str) -> str:
    """
    Extract region name from AWS RDS endpoint.
    Example: database-1.abc123xyz.us-east-1.rds.amazonaws.com -> us-east-1

    Args:
        hostname (str): The RDS host endpoint

    Returns:
        str: AWS region name
    """
    parts = hostname.split(".")
    for part in parts:
        if part.startswith(("us-", "eu-", "ap-", "ca-", "me-", "sa-", "af-")):
            return part
    raise ValueError(f"Could not find valid AWS region in hostname: {hostname}")


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
