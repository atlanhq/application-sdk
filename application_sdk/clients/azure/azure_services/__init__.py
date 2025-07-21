"""
Azure service-specific clients for the application-sdk framework.

This module provides service-specific client implementations for Azure Storage services
including Blob Storage and Data Lake Storage Gen2.
"""

from .storage import AzureStorageClient

__all__ = [
    "AzureStorageClient",
]
