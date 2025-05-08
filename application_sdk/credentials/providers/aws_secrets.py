"""AWS Secrets Manager credential provider implementation."""

import traceback
from typing import Any, Dict

from application_sdk.common.utils import parse_credentials_extra
from application_sdk.credentials.base import CredentialError, CredentialProvider
from application_sdk.credentials.credentials_utils import process_secret_data, apply_secret_values
from application_sdk.common.logger_adaptors import get_logger
logger = get_logger(__name__)


class AWSSecretsManagerCredentialProvider(CredentialProvider):
    """Provider for credentials from AWS Secrets Manager."""

    async def get_credentials(self, source_credentials: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch credentials from AWS Secrets Manager.

        Args:
            source_credentials (Dict[str, Any]): Credentials containing AWS Secret ARN 
                and region information.

        Returns:
            Dict[str, Any]: Resolved credentials with actual username and password.

        Raises:
            CredentialError: If AWS Secret ARN or region is missing, or if there's 
                an error fetching the secret.
        """
        try:
            # Extract and validate parameters
            params = self._validate_params(source_credentials)
            
            # Fetch secret from AWS Secrets Manager
            secret_data = await self._fetch_secret(
                params["aws_secret_arn"], 
                params["aws_secret_region"], 
                params["secret_store"]
            )
            
            # Apply the secret values to the credentials
            return apply_secret_values(source_credentials, secret_data)
            
        except Exception as e:
            logger.error(f"Error fetching credentials from AWS Secrets Manager: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise CredentialError(f"Failed to fetch credentials from AWS Secrets Manager: {str(e)}")

    def _validate_params(self, source_credentials: Dict[str, Any]) -> Dict[str, str]:
        """
        Extract and validate parameters from source credentials.
        
        Args:
            source_credentials (Dict[str, Any]): Source credentials.
            
        Returns:
            Dict[str, str]: A dictionary containing validated parameters
            
        Raises:
            CredentialError: If required parameters are missing.
        """
        extra = parse_credentials_extra(source_credentials)
        params = {
            "aws_secret_arn": extra.get("aws_secret_arn"),
            "aws_secret_region": extra.get("aws_secret_region"),
            "secret_store": extra.get("secret_store", "aws-secrets"),
            # New parameters can be added here in the future
        }
        
        # Validate required parameters
        if not params["aws_secret_arn"]:
            raise CredentialError("aws_secret_arn is required for AWS Secrets Manager")

        if not params["aws_secret_region"]:
            raise CredentialError("aws_secret_region is required for AWS Secrets Manager")
            
        return params
    
    async def _fetch_secret(self, aws_secret_arn: str, aws_secret_region: str, 
                          secret_store: str) -> Dict[str, Any]:
        """
        Fetch secret from AWS Secrets Manager using Dapr.
        
        Args:
            aws_secret_arn (str): AWS Secret ARN.
            aws_secret_region (str): AWS region.
            secret_store (str): Secret store name.
            
        Returns:
            Dict[str, Any]: Secret data.
            
        Raises:
            CredentialError: If there's an error fetching the secret.
        """
        from dapr.clients import DaprClient
        
        secret_store_region = f"{secret_store}-{aws_secret_region}"
        
        with DaprClient() as client:
            secret = client.get_secret(
                store_name=secret_store_region, 
                key=aws_secret_arn
            )
            
            return process_secret_data(secret.secret)