import boto3
from botocore.exceptions import ClientError

def get_aws_role_rds_token(role_arn: str, external_id: str = None, session_name: str = "temp-session", region: str = "ap-south-1", host: str, port: int = 5432, user: str) -> str:
    """
    Get temporary AWS credentials by assuming a role.
    
    Args:
        role_arn (str): The ARN of the role to assume
        external_id (str): The external ID to use for the session
        session_name (str): Name of the temporary session
        
    Returns:
        dict: Dictionary containing AWS credentials (access key, secret key, session token)
    """
    try:
        sts_client = boto3.client('sts')
        assumed_role = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            ExternalId=external_id
        )
        
        credentials = assumed_role['Credentials']
        aws_client = boto3.client(
                "rds",
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                region_name=region,
            )
        token = aws_client.generate_db_auth_token(
                DBHostname=host, Port=port, DBUsername=user
            )
        return token
        
    except ClientError as e:
        raise Exception(f"Failed to assume role: {str(e)}")

def get_aws_user_rds_token(aws_access_key_id: str, aws_secret_access_key: str, region: str = "ap-south-1", host: str, port: int = 5432, user: str) -> str:
    """
    Get AWS credentials using IAM user credentials.
    
    Args:
        aws_access_key_id (str): AWS access key ID
        aws_secret_access_key (str): AWS secret access key
        region (str): AWS region (default: us-east-1)
        
    Returns:
        dict: Dictionary containing AWS credentials
    """
    try:
        aws_client = boto3.client(
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