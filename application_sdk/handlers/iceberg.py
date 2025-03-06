"""
Iceberg handler for orchestrating Iceberg data extraction operations.

This module provides a handler for Iceberg operations, with methods for
performing authentication, preflight checks, and fetching metadata.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

import pandas as pd

from application_sdk.application.fastapi.models import MetadataType
from application_sdk.clients.iceberg import IcebergClient
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.decorators import transform
from application_sdk.handlers import HandlerInterface
from application_sdk.inputs.iceberg import IcebergInput

logger = get_logger(__name__)


class IcebergConstants(Enum):
    """Constants for Iceberg handler"""
    NAMESPACE_KEY = "namespace"
    TABLE_KEY = "table"


class IcebergHandler(HandlerInterface):
    """
    Handler class for Iceberg data extraction workflows.
    
    This class provides functionality for orchestrating operations on Iceberg tables,
    including authentication, preflight checks, and metadata retrieval.
    
    Attributes:
        iceberg_client (IcebergClient): Client for connecting to Iceberg catalogs.
        test_catalog_access_query (Dict[str, Any]): Query to test catalog access.
    """

    iceberg_client: IcebergClient
    # Check for catalog namespace listing capability as authentication test
    test_catalog_access_query: Dict[str, Any] = {
        "test_catalog": True,
        "test_namespace": True,
    }

    def __init__(self, iceberg_client: Optional[IcebergClient] = None):
        """Initialize the Iceberg handler.
        
        Args:
            iceberg_client (Optional[IcebergClient], optional): Client for connecting to 
                Iceberg catalogs. Defaults to None.
        """
        self.iceberg_client = iceberg_client if iceberg_client else IcebergClient()

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load and initialize the Iceberg client.
        
        Args:
            credentials (Dict[str, Any]): Connection credentials.
        """
        await self.iceberg_client.load(credentials)

    @transform(iceberg_table=IcebergInput(table=None))
    async def test_auth(
        self, 
        iceberg_table: pd.DataFrame = None,
        **kwargs: Any
    ) -> bool:
        """Test the authentication credentials.
        
        This method performs a comprehensive test of catalog access, including:
        1. Validating catalog connection
        2. Testing namespace listing capability
        3. Testing table listing capability if namespace is configured
        
        Returns:
            bool: True if authentication is successful, False otherwise.
            
        Raises:
            Exception: If authentication fails.
        """
        if not self.iceberg_client.catalog:
            raise ValueError("Iceberg client is not initialized")
            
        try:
            # Test 1: Basic catalog access
            await self._test_catalog_access()
            
            # Test 2: Namespace access if configured
            namespace = self.iceberg_client.namespace
            if namespace:
                await self._test_namespace_access(namespace)
                
                # Test 3: Table listing capability
                await self._test_table_listing_access(namespace)
                
            return True
        except Exception as e:
            logger.error(f"Authentication test failed: {str(e)}")
            raise

    async def _test_catalog_access(self) -> bool:
        """Test basic catalog access.
        
        Returns:
            bool: True if catalog access is successful.
            
        Raises:
            ValueError: If catalog access fails.
        """
        try:
            # Test catalog access by listing namespaces
            self.iceberg_client.catalog.list_namespaces()
            logger.info("Catalog access test successful")
            return True
        except Exception as e:
            logger.error(f"Catalog access test failed: {str(e)}")
            raise ValueError(f"Catalog access test failed: {str(e)}")
            
    async def _test_namespace_access(self, namespace: str) -> bool:
        """Test namespace access by validating it exists.
        
        Args:
            namespace (str): Namespace to test.
            
        Returns:
            bool: True if namespace access is successful.
            
        Raises:
            ValueError: If namespace access fails.
        """
        try:
            # Check if namespace exists by getting all namespaces
            namespaces = self.iceberg_client.catalog.list_namespaces()
            namespace_exists = False
            
            for ns in namespaces:
                # Namespaces can be returned as tuples in some catalog implementations
                ns_str = ns[0] if isinstance(ns, tuple) else ns
                if ns_str == namespace:
                    namespace_exists = True
                    break
                    
            if not namespace_exists:
                raise ValueError(f"Namespace '{namespace}' does not exist or is not accessible")
                
            logger.info(f"Namespace access test successful for namespace '{namespace}'")
            return True
        except Exception as e:
            logger.error(f"Namespace access test failed: {str(e)}")
            raise ValueError(f"Namespace access test failed: {str(e)}")
            
    async def _test_table_listing_access(self, namespace: str) -> bool:
        """Test table listing access for a namespace.
        
        Args:
            namespace (str): Namespace to test.
            
        Returns:
            bool: True if table listing access is successful.
            
        Raises:
            ValueError: If table listing access fails.
        """
        try:
            # Test table listing capability
            self.iceberg_client.catalog.list_tables(namespace)
            logger.info(f"Table listing access test successful for namespace '{namespace}'")
            return True
        except Exception as e:
            logger.error(f"Table listing access test failed: {str(e)}")
            raise ValueError(f"Table listing access test failed: {str(e)}")

    async def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Perform preflight checks to ensure table access.
        
        This method performs a comprehensive check of access permissions:
        1. Validates catalog access
        2. Verifies namespace exists and is accessible
        3. Checks if the specified table exists and is accessible
        
        Args:
            payload (Dict[str, Any]): The payload containing table information.
            
        Returns:
            Dict[str, Any]: Result of the preflight check.
            
        Raises:
            ValueError: If the check fails.
        """
        if not self.iceberg_client.catalog:
            raise ValueError("Iceberg client is not initialized")
            
        try:
            # Extract table info from payload
            namespace = payload.get("namespace", self.iceberg_client.namespace)
            table = payload.get("table")
            
            if not namespace:
                raise ValueError("Namespace is not specified")
                
            if not table:
                raise ValueError("Table is not specified")
                
            # Step 1: Verify catalog access
            await self._test_catalog_access()
            
            # Step 2: Verify namespace access
            await self._test_namespace_access(namespace)
            
            # Step 3: Check if the table exists
            table_exists = False
            tables = self.iceberg_client.catalog.list_tables(namespace)
            for t in tables:
                if t.split(".")[-1] == table:
                    table_exists = True
                    break
                    
            if not table_exists:
                raise ValueError(f"Table {namespace}.{table} does not exist or is not accessible")
                
            # Step 4: Verify table metadata can be loaded
            try:
                self.iceberg_client.catalog.load_table(f"{namespace}.{table}")
            except Exception as e:
                raise ValueError(f"Cannot load table metadata for {namespace}.{table}: {str(e)}")
                
            logger.info(f"Preflight check successful for table {namespace}.{table}")
            return {
                "status": "success",
                "namespace": namespace,
                "table": table,
                "message": f"Table {namespace}.{table} is accessible"
            }
        except Exception as e:
            logger.error(f"Preflight check failed: {str(e)}")
            raise ValueError(f"Preflight check failed: {str(e)}")

    async def fetch_metadata(
        self,
        metadata_type: Optional[MetadataType] = None,
        catalog: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Fetch metadata based on the requested type.
        
        Args:
            metadata_type (Optional[MetadataType], optional): Type of metadata to fetch.
                Defaults to None.
            catalog (Optional[str], optional): Catalog name. Defaults to None.
            
        Returns:
            List[Dict[str, str]]: List of metadata dictionaries.
            
        Raises:
            ValueError: If metadata_type is invalid or catalog is required but not provided.
        """
        if not self.iceberg_client.catalog:
            raise ValueError("Iceberg client is not initialized")
            
        try:
            if metadata_type == MetadataType.ALL:
                # Fetch both namespaces and tables
                result = []
                
                # Get all namespaces
                namespaces = self.iceberg_client.catalog.list_namespaces()
                
                # For each namespace, get all tables
                for namespace in namespaces:
                    ns_str = namespace[0] if isinstance(namespace, tuple) else namespace
                    tables = self.iceberg_client.catalog.list_tables(ns_str)
                    
                    # Add each table to the result with its namespace
                    for table in tables:
                        table_name = table.split(".")[-1]
                        result.append({
                            "namespace": ns_str,
                            "table": table_name
                        })
                        
                return result
                
            elif metadata_type == MetadataType.DATABASE:
                # In Iceberg, databases are similar to namespaces
                namespaces = self.iceberg_client.catalog.list_namespaces()
                return [{"namespace": ns[0] if isinstance(ns, tuple) else ns} for ns in namespaces]
                
            elif metadata_type == MetadataType.SCHEMA:
                # In Iceberg context, schema refers to table structure within a namespace
                if not catalog:  # using catalog as namespace parameter
                    raise ValueError("Namespace must be specified when fetching tables")
                    
                tables = self.iceberg_client.catalog.list_tables(catalog)
                return [{"namespace": catalog, "table": table.split(".")[-1]} for table in tables]
                
            else:
                raise ValueError(f"Invalid metadata type: {metadata_type}")
                
        except Exception as e:
            logger.error(f"Failed to fetch metadata: {str(e)}")
            raise

    async def fetch_table_schema(self, namespace: str, table: str) -> Dict[str, Any]:
        """Fetch the schema of a specific table.
        
        Args:
            namespace (str): Namespace of the table.
            table (str): Name of the table.
            
        Returns:
            Dict[str, Any]: Schema information of the table.
            
        Raises:
            ValueError: If the table doesn't exist.
        """
        if not self.iceberg_client.catalog:
            raise ValueError("Iceberg client is not initialized")
            
        try:
            # First verify namespace and table access
            await self._test_namespace_access(namespace)
            
            # Load the table
            table_obj = self.iceberg_client.catalog.load_table(f"{namespace}.{table}")
            
            # Get the schema
            schema = table_obj.schema()
            
            # Convert schema to a dictionary
            fields = []
            for field in schema.fields:
                fields.append({
                    "name": field.name,
                    "type": str(field.type),
                    "required": not field.optional,
                    "doc": field.doc if field.doc else ""
                })
                
            return {
                "namespace": namespace,
                "table": table,
                "fields": fields
            }
            
        except Exception as e:
            logger.error(f"Failed to fetch table schema: {str(e)}")
            raise 