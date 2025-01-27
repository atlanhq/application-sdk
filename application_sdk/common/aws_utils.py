from boto3 import client # type: ignore
from botocore.exceptions import ClientError

def get_aws_role_rds_token(role_arn: str, host: str, user: str, external_id: str | None = None, session_name: str = "temp-session", region: str = "ap-south-1", port: int = 5432) -> str:
    """
    Get temporary AWS credentials by assuming a role and generate RDS auth token.
    
    Args:
        role_arn (str): The ARN of the role to assume
        host (str): The RDS host endpoint
        user (str): The database username
        external_id (str, optional): The external ID to use for the session
        session_name (str, optional): Name of the temporary session
        region (str, optional): AWS region
        port (int, optional): Database port
        
    Returns:
        str: RDS authentication token
    """
    try:
        sts_client = client('sts')
        assumed_role = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            ExternalId=external_id or ""
        )
        
        credentials = assumed_role['Credentials']
        aws_client = client(
                "rds",
                aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'],
                aws_session_token=credentials['SessionToken'],
                region_name=region,
            )
        token: str = aws_client.generate_db_auth_token(
                DBHostname=host, Port=port, DBUsername=user
            )
        return token
        
    except ClientError as e:
        raise Exception(f"Failed to assume role: {str(e)}")

def get_aws_user_rds_token(aws_access_key_id: str, aws_secret_access_key: str, host: str, user: str, region: str = "ap-south-1", port: int = 5432) -> str:
    """
    Generate RDS auth token using IAM user credentials.
    
    Args:
        aws_access_key_id (str): AWS access key ID
        aws_secret_access_key (str): AWS secret access key
        host (str): The RDS host endpoint
        user (str): The database username
        region (str, optional): AWS region
        port (int, optional): Database port
        
    Returns:
        str: RDS authentication token
    """
    try:
        aws_client= client(
                "rds",
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                region_name=region,
            )
        token = aws_client.generate_db_auth_token(
                DBHostname=host, Port=port, DBUsername=user
            )
        return token
    except Exception as e:
        raise Exception(f"Failed to get user credentials: {str(e)}")