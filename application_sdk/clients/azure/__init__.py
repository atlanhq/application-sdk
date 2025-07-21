"""
Azure client module for the application-sdk framework.

This module provides a unified interface for connecting to and interacting
with Azure Storage services including Blob Storage and Data Lake Storage Gen2.

The module includes:
- AzureClient: Main client for unified Azure service access
- AzureAuthProvider: Authentication provider for different Azure auth methods
- AzureStorageClient: Storage service client for Blob and Data Lake Storage
- Utilities: Common Azure helper functions
"""

from .azure import AzureClient
from .azure_auth import AzureAuthProvider
from .azure_services import AzureStorageClient
from .azure_utils import (
    parse_azure_url,
    validate_azure_credentials,
    build_azure_connection_string,
    extract_azure_resource_info,
    validate_azure_permissions,
    get_azure_service_endpoint,
    format_azure_error_message,
)

__all__ = [
    # Main client
    "AzureClient",
    
    # Authentication
    "AzureAuthProvider",
    
    # Service-specific clients
    "AzureStorageClient",
    
    # Utilities
    "parse_azure_url",
    "validate_azure_credentials",
    "build_azure_connection_string",
    "extract_azure_resource_info",
    "validate_azure_permissions",
    "get_azure_service_endpoint",
    "format_azure_error_message",
]

__version__ = "0.1.0" 