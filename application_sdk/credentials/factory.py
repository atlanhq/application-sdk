"""Factory for creating credential providers."""

from typing import Dict, Type

from application_sdk.credentials.base import CredentialError, CredentialProvider


class CredentialProviderFactory:
    """Factory for creating credential providers based on credential source."""
    
    # Storage for registered providers
    _providers: Dict[str, Type[CredentialProvider]] = {}
    
    @classmethod
    def register_provider(cls, source_type: str, provider_class: Type[CredentialProvider]):
        """
        Register a new credential provider.
        
        Args:
            source_type (str): The credential source type identifier.
            provider_class (Type[CredentialProvider]): The provider class to register.
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
            CredentialError: If the source type is not supported.
        """
        provider_class = cls._providers.get(source_type)
        if not provider_class:
            raise CredentialError(f"Unsupported credential source: {source_type}")
        
        return provider_class()