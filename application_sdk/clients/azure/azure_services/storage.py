"""
Azure Storage client implementation.

This module provides the AzureStorageClient class for connecting to and interacting
with Azure Storage services including Blob Storage and Data Lake Storage Gen2.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Union

from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.storage.blob import BlobServiceClient
from azure.storage.filedatalake import DataLakeServiceClient

from application_sdk.clients import ClientInterface
from application_sdk.common.error_codes import ClientError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class AzureStorageClient(ClientInterface):
    """
    Azure Storage client for metadata extraction.

    This client supports both Azure Blob Storage and Azure Data Lake Storage Gen2,
    with authentication via Azure credentials and support for both public endpoints
    and private links.

    Attributes:
        credential (TokenCredential): Azure credential instance
        _blob_clients (Dict[str, BlobServiceClient]): Cache of blob service clients
        _datalake_clients (Dict[str, DataLakeServiceClient]): Cache of data lake clients
        _executor (ThreadPoolExecutor): Thread pool for async operations
        _connection_health (bool): Connection health status
    """

    # Service types
    SERVICE_TYPE_BLOB = "blob"
    SERVICE_TYPE_GEN2 = "gen2"

    # Network configuration types
    NETWORK_CONFIG_PUBLIC = "public_endpoint"
    NETWORK_CONFIG_PRIVATE = "private_link"

    # URL templates
    AZURE_BLOB_URL_TEMPLATE = "https://{account_name}.blob.core.windows.net"
    AZURE_DATALAKE_URL_TEMPLATE = "https://{account_name}.dfs.core.windows.net"

    def __init__(
        self,
        credential: TokenCredential,
        network_config: str = NETWORK_CONFIG_PUBLIC,
        timeout_seconds: int = 300,
        max_workers: int = 10,
        **kwargs: Any,
    ):
        """
        Initialize the Azure Storage client.

        Args:
            credential (TokenCredential): Azure credential for authentication
            network_config (str): Network configuration (public_endpoint or private_link)
            timeout_seconds (int): Request timeout in seconds
            max_workers (int): Maximum number of worker threads
            **kwargs: Additional keyword arguments
        """
        self.credential = credential
        self.network_config = network_config
        self.timeout_seconds = timeout_seconds
        self._blob_clients: Dict[str, BlobServiceClient] = {}
        self._datalake_clients: Dict[str, DataLakeServiceClient] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._connection_health = False
        self._kwargs = kwargs

    async def load(self, credentials: Dict[str, Any]) -> None:
        """
        Load and establish Azure Storage connection.

        Args:
            credentials (Dict[str, Any]): Azure Storage connection credentials.

        Raises:
            ClientError: If connection fails due to authentication or connection issues
        """
        try:
            logger.info("Loading Azure Storage client...")

            # Test the credential
            await self._test_credential()

            self._connection_health = True
            logger.info("Azure Storage client loaded successfully")

        except ClientAuthenticationError as e:
            logger.error(f"Azure Storage authentication failed: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")
        except AzureError as e:
            logger.error(f"Azure Storage connection error: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error loading Azure Storage client: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")

    async def close(self) -> None:
        """Close Azure Storage connections and clean up resources."""
        try:
            logger.info("Closing Azure Storage client...")

            # Close blob clients
            for client in self._blob_clients.values():
                await self._close_client(client)

            # Close data lake clients
            for client in self._datalake_clients.values():
                await self._close_client(client)

            # Clear caches
            self._blob_clients.clear()
            self._datalake_clients.clear()

            # Shutdown executor
            self._executor.shutdown(wait=True)

            # Reset connection health
            self._connection_health = False

            logger.info("Azure Storage client closed successfully")

        except Exception as e:
            logger.error(f"Error closing Azure Storage client: {str(e)}")

    async def get_service_client(
        self, storage_account_name: str, private_link: Optional[str] = None
    ) -> tuple[Union[BlobServiceClient, DataLakeServiceClient], str]:
        """
        Get Azure service client for a storage account.

        Args:
            storage_account_name (str): Name of the storage account
            private_link (Optional[str]): Private link URL (if using private endpoint)

        Returns:
            tuple: (service_client, service_type)

        Raises:
            ClientError: If client is not loaded or client creation fails
        """
        if not self._connection_health:
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: Client not loaded")

        try:
            # Determine account URL
            if private_link and self.network_config == self.NETWORK_CONFIG_PRIVATE:
                account_url = private_link
            else:
                # Try Data Lake Gen2 first, fallback to Blob Storage
                account_url = self.AZURE_DATALAKE_URL_TEMPLATE.format(
                    account_name=storage_account_name
                )

            # Try to create Data Lake Gen2 client first
            try:
                client = DataLakeServiceClient(
                    account_url=account_url, credential=self.credential
                )

                # Test the connection
                await self._test_datalake_client(client)

                self._datalake_clients[storage_account_name] = client
                logger.debug(f"Using Data Lake Gen2 client for {storage_account_name}")
                return client, self.SERVICE_TYPE_GEN2

            except AzureError:
                # Fallback to Blob Storage
                blob_url = self.AZURE_BLOB_URL_TEMPLATE.format(
                    account_name=storage_account_name
                )

                client = BlobServiceClient(
                    account_url=blob_url, credential=self.credential
                )

                # Test the connection
                await self._test_blob_client(client)

                self._blob_clients[storage_account_name] = client
                logger.debug(f"Using Blob Storage client for {storage_account_name}")
                return client, self.SERVICE_TYPE_BLOB

        except Exception as e:
            logger.error(
                f"Failed to create service client for {storage_account_name}: {e}"
            )
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")

    async def list_containers(
        self,
        storage_account_name: str,
        container_prefix: str = "",
        private_link: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List containers in a storage account.

        Args:
            storage_account_name (str): Name of the storage account
            container_prefix (str): Prefix to filter containers
            private_link (Optional[str]): Private link URL

        Returns:
            List[Dict[str, Any]]: List of container information

        Raises:
            ClientError: If operation fails
        """
        try:
            client, service_type = await self.get_service_client(
                storage_account_name, private_link
            )

            if service_type == self.SERVICE_TYPE_GEN2:
                return await self._list_datalake_containers(client, container_prefix)
            else:
                return await self._list_blob_containers(client, container_prefix)

        except Exception as e:
            logger.error(f"Failed to list containers: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")

    async def list_objects(
        self,
        storage_account_name: str,
        container_name: str,
        object_prefix: str = "",
        include_folders: bool = False,
        batch_size: int = 1000,
        private_link: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List objects in a container.

        Args:
            storage_account_name (str): Name of the storage account
            container_name (str): Name of the container
            object_prefix (str): Prefix to filter objects
            include_folders (bool): Whether to include folder objects
            batch_size (int): Number of objects to retrieve per batch
            private_link (Optional[str]): Private link URL

        Returns:
            List[Dict[str, Any]]: List of object information

        Raises:
            ClientError: If operation fails
        """
        try:
            client, service_type = await self.get_service_client(
                storage_account_name, private_link
            )

            if service_type == self.SERVICE_TYPE_GEN2:
                return await self._list_datalake_objects(
                    client, container_name, object_prefix, include_folders, batch_size
                )
            else:
                return await self._list_blob_objects(
                    client, container_name, object_prefix, include_folders, batch_size
                )

        except Exception as e:
            logger.error(f"Failed to list objects: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")

    async def get_object_properties(
        self,
        storage_account_name: str,
        container_name: str,
        object_name: str,
        private_link: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get object properties.

        Args:
            storage_account_name (str): Name of the storage account
            container_name (str): Name of the container
            object_name (str): Name of the object
            private_link (Optional[str]): Private link URL

        Returns:
            Dict[str, Any]: Object properties

        Raises:
            ClientError: If operation fails
        """
        try:
            client, service_type = await self.get_service_client(
                storage_account_name, private_link
            )

            if service_type == self.SERVICE_TYPE_GEN2:
                return await self._get_datalake_object_properties(
                    client, container_name, object_name
                )
            else:
                return await self._get_blob_object_properties(
                    client, container_name, object_name
                )

        except Exception as e:
            logger.error(f"Failed to get object properties: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")

    async def health_check(self) -> Dict[str, Any]:
        """
        Perform health check on Azure Storage connection.

        Returns:
            Dict[str, Any]: Health status information.
        """
        health_status = {
            "status": "unknown",
            "connection_health": self._connection_health,
            "blob_clients": len(self._blob_clients),
            "datalake_clients": len(self._datalake_clients),
            "overall_health": False,
        }

        if not self._connection_health:
            health_status["status"] = "unhealthy"
            return health_status

        # Test credential if available
        if self.credential:
            try:
                await self._test_credential()
                health_status["credential_health"] = True
                health_status["status"] = "healthy"
            except Exception as e:
                health_status["credential_health"] = False
                health_status["credential_error"] = str(e)
                health_status["status"] = "unhealthy"
        else:
            health_status["status"] = "unknown"

        health_status["overall_health"] = health_status.get("credential_health", False)

        return health_status

    async def _test_credential(self) -> None:
        """Test the Azure credential."""
        try:
            await asyncio.get_event_loop().run_in_executor(
                self._executor,
                self.credential.get_token,
                "https://storage.azure.com/.default",
            )
        except Exception as e:
            raise ClientAuthenticationError(f"Credential test failed: {str(e)}")

    async def _test_blob_client(self, client: BlobServiceClient) -> None:
        """Test blob client connection."""
        try:
            await asyncio.get_event_loop().run_in_executor(
                self._executor, client.get_service_properties
            )
        except Exception as e:
            raise AzureError(f"Blob client test failed: {str(e)}")

    async def _test_datalake_client(self, client: DataLakeServiceClient) -> None:
        """Test data lake client connection."""
        try:
            await asyncio.get_event_loop().run_in_executor(
                self._executor, client.get_service_properties
            )
        except Exception as e:
            raise AzureError(f"Data Lake client test failed: {str(e)}")

    async def _list_blob_containers(
        self, client: BlobServiceClient, container_prefix: str
    ) -> List[Dict[str, Any]]:
        """List blob containers."""
        try:

            def _list_containers_sync():
                containers = []
                for container in client.list_containers(
                    name_starts_with=container_prefix
                ):
                    containers.append(
                        {
                            "name": container.name,
                            "last_modified": container.last_modified,
                            "etag": container.etag,
                            "lease_status": container.lease_status,
                            "lease_state": container.lease_state,
                            "public_access": container.public_access,
                            "has_immutability_policy": container.has_immutability_policy,
                            "has_legal_hold": container.has_legal_hold,
                            "metadata": container.metadata,
                        }
                    )
                return containers

            # Run the synchronous operation in a thread pool
            loop = asyncio.get_event_loop()
            containers = await loop.run_in_executor(
                self._executor, _list_containers_sync
            )
            return containers
        except Exception as e:
            logger.error(f"Failed to list blob containers: {str(e)}")
            raise

    async def _list_datalake_containers(
        self, client: DataLakeServiceClient, container_prefix: str
    ) -> List[Dict[str, Any]]:
        """List data lake containers."""
        try:

            def _list_containers_sync():
                containers = []
                for container in client.list_file_systems(
                    name_starts_with=container_prefix
                ):
                    containers.append(
                        {
                            "name": container.name,
                            "last_modified": container.last_modified,
                            "etag": container.etag,
                            "lease_status": getattr(container, "lease_status", None),
                            "lease_state": getattr(container, "lease_state", None),
                            "public_access": getattr(container, "public_access", None),
                            "has_immutability_policy": getattr(
                                container, "has_immutability_policy", None
                            ),
                            "has_legal_hold": getattr(
                                container, "has_legal_hold", None
                            ),
                            "metadata": getattr(container, "metadata", {}),
                        }
                    )
                return containers

            # Run the synchronous operation in a thread pool
            loop = asyncio.get_event_loop()
            containers = await loop.run_in_executor(
                self._executor, _list_containers_sync
            )
            return containers
        except Exception as e:
            logger.error(f"Failed to list data lake containers: {str(e)}")
            raise

    async def _list_blob_objects(
        self,
        client: BlobServiceClient,
        container_name: str,
        object_prefix: str,
        include_folders: bool,
        batch_size: int,
    ) -> List[Dict[str, Any]]:
        """List blob objects."""
        try:

            def _list_objects_sync():
                objects = []
                container_client = client.get_container_client(container_name)

                for blob in container_client.list_blobs(
                    name_starts_with=object_prefix, include=["metadata"]
                ):
                    objects.append(
                        {
                            "name": blob.name,
                            "size": blob.size,
                            "last_modified": blob.last_modified,
                            "etag": blob.etag,
                            "content_type": blob.content_settings.content_type,
                            "content_encoding": blob.content_settings.content_encoding,
                            "content_language": blob.content_settings.content_language,
                            "content_disposition": blob.content_settings.content_disposition,
                            "cache_control": blob.content_settings.cache_control,
                            "lease_status": blob.lease_status,
                            "lease_state": blob.lease_state,
                            "lease_duration": blob.lease_duration,
                            "copy_id": blob.copy_id,
                            "copy_source": blob.copy_source,
                            "copy_progress": blob.copy_progress,
                            "copy_completion_time": blob.copy_completion_time,
                            "copy_status": blob.copy_status,
                            "copy_status_description": blob.copy_status_description,
                            "server_encrypted": blob.server_encrypted,
                            "incremental_copy": blob.incremental_copy,
                            "metadata": blob.metadata,
                        }
                    )

                    if len(objects) >= batch_size:
                        break

                return objects

            # Run the synchronous operation in a thread pool
            loop = asyncio.get_event_loop()
            objects = await loop.run_in_executor(self._executor, _list_objects_sync)
            return objects
        except Exception as e:
            logger.error(f"Failed to list blob objects: {str(e)}")
            raise

    async def _list_datalake_objects(
        self,
        client: DataLakeServiceClient,
        container_name: str,
        object_prefix: str,
        include_folders: bool,
        batch_size: int,
    ) -> List[Dict[str, Any]]:
        """List data lake objects."""
        try:

            def _list_objects_sync():
                objects = []
                file_system_client = client.get_file_system_client(container_name)

                for path in file_system_client.get_paths(
                    path=object_prefix, recursive=True
                ):
                    objects.append(
                        {
                            "name": path.name,
                            "size": path.content_length,
                            "last_modified": path.last_modified,
                            "etag": path.etag,
                            "owner": path.owner,
                            "group": path.group,
                            "permissions": path.permissions,
                            "is_directory": path.is_directory,
                            "encryption_context": path.encryption_context,
                        }
                    )

                    if len(objects) >= batch_size:
                        break

                return objects

            # Run the synchronous operation in a thread pool
            loop = asyncio.get_event_loop()
            objects = await loop.run_in_executor(self._executor, _list_objects_sync)
            return objects
        except Exception as e:
            logger.error(f"Failed to list data lake objects: {str(e)}")
            raise

    async def _get_blob_object_properties(
        self, client: BlobServiceClient, container_name: str, object_name: str
    ) -> Dict[str, Any]:
        """Get blob object properties."""
        try:
            blob_client = client.get_blob_client(container_name, object_name)
            properties = await asyncio.get_event_loop().run_in_executor(
                self._executor, blob_client.get_blob_properties
            )

            return {
                "name": properties.name,
                "size": properties.size,
                "last_modified": properties.last_modified,
                "etag": properties.etag,
                "content_type": properties.content_settings.content_type,
                "content_encoding": properties.content_settings.content_encoding,
                "content_language": properties.content_settings.content_language,
                "content_disposition": properties.content_settings.content_disposition,
                "cache_control": properties.content_settings.cache_control,
                "lease_status": properties.lease_status,
                "lease_state": properties.lease_state,
                "lease_duration": properties.lease_duration,
                "copy_id": properties.copy_id,
                "copy_source": properties.copy_source,
                "copy_progress": properties.copy_progress,
                "copy_completion_time": properties.copy_completion_time,
                "copy_status": properties.copy_status,
                "copy_status_description": properties.copy_status_description,
                "server_encrypted": properties.server_encrypted,
                "incremental_copy": properties.incremental_copy,
                "metadata": properties.metadata,
            }
        except Exception as e:
            logger.error(f"Failed to get blob object properties: {str(e)}")
            raise

    async def _get_datalake_object_properties(
        self, client: DataLakeServiceClient, container_name: str, object_name: str
    ) -> Dict[str, Any]:
        """Get data lake object properties."""
        try:
            file_client = client.get_file_client(container_name, object_name)
            properties = await asyncio.get_event_loop().run_in_executor(
                self._executor, file_client.get_file_properties
            )

            return {
                "name": properties.name,
                "size": getattr(
                    properties, "content_length", getattr(properties, "size", None)
                ),
                "last_modified": properties.last_modified,
                "etag": properties.etag,
                "owner": getattr(properties, "owner", None),
                "group": getattr(properties, "group", None),
                "permissions": getattr(properties, "permissions", None),
                "is_directory": getattr(properties, "is_directory", None),
                "encryption_context": getattr(properties, "encryption_context", None),
            }
        except Exception as e:
            logger.error(f"Failed to get data lake object properties: {str(e)}")
            raise

    async def _close_client(self, client: Any) -> None:
        """Close a service client."""
        try:
            if hasattr(client, "close"):
                await asyncio.get_event_loop().run_in_executor(
                    self._executor, client.close
                )
        except Exception as e:
            logger.warning(f"Error closing client: {str(e)}")
