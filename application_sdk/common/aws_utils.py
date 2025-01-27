import boto3
from botocore.exceptions import ClientError

def get_aws_role_credentials(role_arn: str, external_id: str = None, session_name: str = "temp-session") -> dict:
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
        return {
            'aws_access_key_id': credentials['AccessKeyId'],
            'aws_secret_access_key': credentials['SecretAccessKey'],
            'aws_session_token': credentials['SessionToken'],
            'expiration': credentials['Expiration']
        }
    except ClientError as e:
        raise Exception(f"Failed to assume role: {str(e)}")

def get_aws_user_credentials(aws_access_key_id: str, aws_secret_access_key: str, region: str = "us-east-1") -> dict:
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
        session = boto3.Session(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region
        )
        
        credentials = session.get_credentials()
        return {
            'aws_access_key_id': credentials.access_key,
            'aws_secret_access_key': credentials.secret_key,
            'region': region
        }
    except Exception as e:
        raise Exception(f"Failed to get user credentials: {str(e)}")
