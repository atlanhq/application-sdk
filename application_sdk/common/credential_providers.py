"""
Credential provider implementation for various credential sources.

This module provides a framework for fetching credentials from different sources
such as direct credentials, AWS Secrets Manager, and extensibility for other
credential sources.
"""

import json
from typing import Any, Dict, Protocol, List
import collections.abc

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.utils import parse_credentials_extra
import traceback

logger = get_logger(__name__)


class CredentialProvider(Protocol):
    """Protocol defining the interface for credential providers."""

    async def get_credentials(self, source_credentials: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get the resolved credentials from the source credentials.

        Args:
            source_credentials (Dict[str, Any]): The source credentials containing
                metadata necessary to resolve actual credentials.

        Returns:
            Dict[str, Any]: The resolved credentials.
        
        Raises:
            ValueError: If credentials cannot be resolved.
        """
        ...


class DirectCredentialProvider(CredentialProvider):
    """Provider for directly supplied credentials."""

    async def get_credentials(self, source_credentials: Dict[str, Any]) -> Dict[str, Any]:
        """
        Return the direct credentials as is.

        Args:
            source_credentials (Dict[str, Any]): The direct credentials.

        Returns:
            Dict[str, Any]: The same credentials.
        """
        return source_credentials


class AWSSecretsManagerCredentialProvider(CredentialProvider):
    """Provider for credentials from AWS Secrets Manager."""

    async def get_credentials(self, source_credentials: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch credentials from AWS Secrets Manager.

        Args:
            source_credentials (Dict[str, Any]): Credentials containing AWS Secret ARN and region information.

        Returns:
            Dict[str, Any]: Resolved credentials with actual username and password.

        Raises:
            ValueError: If AWS Secret ARN or region is missing, or if there's an error fetching the secret.
        """
        extra = parse_credentials_extra(source_credentials)
        aws_secret_arn = extra.get("aws_secret_arn")
        aws_secret_region = extra.get("aws_secret_region")
        secret_store = extra.get("secret_store", "aws-secrets")  # Default to "aws-secrets" or set as needed
        
        component_metadata = {
            "region": aws_secret_region  # Use the region from credentials
        }

        if not aws_secret_arn:
            raise ValueError("aws_secret_arn is required for AWS Secrets Manager")

        if not aws_secret_region:
            raise ValueError("aws_secret_region is required for AWS Secrets Manager")

        try:
            from dapr.clients import DaprClient

            # Create a Dapr client
            with DaprClient() as client:

                secret = client.get_secret(store_name=secret_store, 
                                           key=aws_secret_arn,
                                           metadata=component_metadata)
                                           
                result_credentials = source_credentials.copy()

                secret_data = secret.secret

                # Convert ScalarMapContainer to dict if needed
                if isinstance(secret_data, collections.abc.Mapping):
                    secret_data = dict(secret_data)

                # If the dict has a single key and its value is a JSON string, parse it
                if (
                    len(secret_data) == 1
                    and isinstance(next(iter(secret_data.values())), str)
                ):
                    try:
                        parsed = json.loads(next(iter(secret_data.values())))
                        if isinstance(parsed, dict):
                            secret_data = parsed
                    except Exception:
                        pass
                
                # Replace credential values with secret values if the credential value exists as a key in the secret store
                # This allows users to enter either direct values or references to secret values
                for key, value in list(result_credentials.items()):
                    if isinstance(value, str) and value in secret_data:
                        result_credentials[key] = secret_data[value]
    
                # # Apply the same secret substitution pattern to the 'extra' dictionary if it exists
                if "extra" in result_credentials and isinstance(result_credentials["extra"], dict):
                    for key, value in list(result_credentials["extra"].items()):
                        if isinstance(value, str) and value in secret_data:
                            result_credentials["extra"][key] = secret_data[value]

                return result_credentials

        except Exception as e:
            logger.error(f"Error fetching credentials from AWS Secrets Manager: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise ValueError(f"Failed to fetch credentials from AWS Secrets Manager: {str(e)}")


class CredentialProviderFactory:
    """Factory for creating credential providers based on credential source."""
    
    # Define _providers with explicit type annotation
    _providers: Dict[str, type] = {
        "direct": DirectCredentialProvider,
        "aws_secrets_manager": AWSSecretsManagerCredentialProvider,
        # Add more providers here as needed
    }
    
    @classmethod
    def register_provider(cls, source_type: str, provider_class: type):
        """
        Register a new credential provider.
        
        Args:
            source_type (str): The credential source type identifier.
            provider_class (type): The provider class to register.
        """
        cls._providers[source_type] = provider_class
    
    @classmethod
    def get_provider(cls, source_type: str) -> CredentialProvider:
        """
        Get the appropriate credential provider for a source type.
        
        Args:
            source_type (str): The credential source type.
            
        Returns:
            CredentialProvider: The credential provider instance.
            
        Raises:
            ValueError: If the source type is not supported.
        """
        provider_class = cls._providers.get(source_type)
        if not provider_class:
            raise ValueError(f"Unsupported credential source: {source_type}")
        
        return provider_class()
    

# the below code is for the UI generation, not being used currently
# Right now we are using the static UI configuration, coded in postgres app
# but i want to make each app agnostic of the credentials code and configuration
# we will use this code to generate the UI configuration for the credentials
class CredentialProviderInfo:
    """Metadata about a credential provider for UI generation."""
    
    def __init__(self, id: str, name: str, description: str, ui_config: Dict[str, Any]):
        self.id = id  # e.g., "aws_secrets_manager"
        self.name = name  # e.g., "AWS Secrets Manager"
        self.description = description  # e.g., "Store credentials in AWS Secrets Manager"
        self.ui_config = ui_config  # Form fields configuration
        
class CredentialProviderRegistry:
    """Registry of available credential providers with UI metadata."""
    
    _providers: Dict[str, CredentialProviderInfo] = {
        "direct": CredentialProviderInfo(
            id="direct",
            name="Direct Input",
            description="Enter credentials directly",
            ui_config={"fields": []}  # No additional fields for direct input
        ),
        "aws_secrets_manager": CredentialProviderInfo(
            id="aws_secrets_manager",
            name="AWS Secrets Manager",
            description="Retrieve credentials from AWS Secrets Manager",
            ui_config={
                "fields": [
                    {
                        "id": "aws-secret-arn",
                        "label": "AWS Secret ARN",
                        "type": "text",
                        "required": True,
                        "placeholder": "arn:aws:secretsmanager:..."
                    },
                    {
                        "id": "aws-secret-region",
                        "label": "AWS Region",
                        "type": "text",
                        "required": True,
                        "placeholder": "us-east-1"
                    }
                ]
            }
        )
        # Add more providers here with their UI configurations
    }
    
    @classmethod
    def get_available_providers(cls) -> List[CredentialProviderInfo]:
        """Get a list of all available credential providers."""
        return list(cls._providers.values())
    
    @classmethod
    def get_provider_info(cls, provider_id: str) -> CredentialProviderInfo:
        """Get information about a specific credential provider."""
        if provider_id not in cls._providers:
            raise ValueError(f"Unknown credential provider: {provider_id}")
        return cls._providers[provider_id]
    
    @classmethod
    def register_provider(cls, provider_info: CredentialProviderInfo):
        """Register a new credential provider."""
        cls._providers[provider_info.id] = provider_info