import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Union

from pyiceberg.catalog import Catalog, load_catalog
from temporalio import activity

from application_sdk.clients import ClientInterface
from application_sdk.common.logger_adaptors import get_logger

activity.logger = get_logger(__name__)


class IcebergClient(ClientInterface):
    """Iceberg client for data extraction operations.

    This class provides functionality for connecting to and querying Iceberg catalogs,
    with support for batch processing and data extraction.

    Attributes:
        catalog: Iceberg catalog instance.
        catalog_uri: URI for connecting to the Iceberg catalog.
        warehouse_path: Path to the Iceberg warehouse.
        namespace: Iceberg namespace.
        credentials (Dict[str, Any]): Connection credentials.
    """

    catalog: Optional[Catalog] = None
    catalog_uri: Optional[str] = None
    warehouse_path: Optional[str] = None
    namespace: Optional[str] = None
    credentials: Dict[str, Any] = {}

    def __init__(
        self,
        catalog_uri: Optional[str] = None,
        warehouse_path: Optional[str] = None,
        namespace: Optional[str] = None,
        credentials: Dict[str, Any] = {},
    ):
        """Initialize the Iceberg client."""
        self.catalog_uri = catalog_uri
        self.warehouse_path = warehouse_path
        self.namespace = namespace
        self.credentials = credentials

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load and establish the Iceberg catalog connection."""
        self.credentials = credentials
        try:
            self.catalog_uri = credentials.get("catalog_uri", self.catalog_uri)
            self.warehouse_path = credentials.get("warehouse_path", self.warehouse_path)
            self.namespace = credentials.get("namespace", self.namespace)
            
            catalog_config = self.get_catalog_connection()
            self.catalog = load_catalog("iceberg", **catalog_config)
            
            # Validate connection by trying to list tables
            if self.namespace:
                self.catalog.list_tables(self.namespace)
            activity.logger.info(f"Successfully connected to Iceberg catalog: {self.catalog_uri}")
            
        except Exception as e:
            activity.logger.error(f"Error loading Iceberg client: {str(e)}")
            self.catalog = None
            raise ValueError(f"Failed to connect to Iceberg catalog: {str(e)}")

    async def close(self) -> None:
        """Close the Iceberg catalog connection."""
        self.catalog = None
        activity.logger.info("Iceberg catalog connection closed")

    def get_catalog_connection(self) -> Dict[str, Any]:
        """Get the catalog connection configuration."""
        if not self.catalog_uri:
            raise ValueError("Catalog URI is required")
        
        config = {
            "uri": self.catalog_uri,
        }
        
        if self.warehouse_path:
            config["warehouse"] = self.warehouse_path
            
        return config

    async def run_query(self, query: str, batch_size: int = 100000):
        """
        Run a query to extract data from Iceberg tables.
        
        The query is expected to be in the format "namespace.table_name",
        or if namespace is configured in the client, just "table_name".

        Args:
            query (str): The table to query in format "namespace.table_name" or "table_name"
            batch_size (int, optional): The number of rows to fetch in each batch.
                Defaults to 100000.

        Yields:
            List of dictionaries containing query results.

        Raises:
            ValueError: If catalog connection is not established or table not found.
            Exception: If the query fails.
        """
        if not self.catalog:
            raise ValueError("Catalog connection is not established")

        activity.logger.info(f"Running query on table: {query}")
        
        try:
            # Parse the query which can be either "namespace.table" or just "table"
            if "." in query:
                namespace, table_name = query.split(".", 1)
            else:
                if not self.namespace:
                    raise ValueError("Namespace is required when table name doesn't include it")
                namespace = self.namespace
                table_name = query
                
            # Load the table
            loop = asyncio.get_running_loop()
            
            with ThreadPoolExecutor() as pool:
                # Load the table
                table = await loop.run_in_executor(
                    pool,
                    lambda: self.catalog.load_table(f"{namespace}.{table_name}")
                )
                
                # Use Daft to read the data
                import daft
                
                # Create a daft dataframe from the table
                df = await loop.run_in_executor(
                    pool,
                    lambda: daft.read_iceberg(table)
                )
                
                # Get total rows
                total_rows = await loop.run_in_executor(pool, df.count_rows)
                activity.logger.info(f"Total rows to process: {total_rows}")
                
                # Process in batches
                offset = 0
                while offset < total_rows:
                    # Slice the data
                    batch_df = await loop.run_in_executor(
                        pool, 
                        lambda: df.limit(batch_size, offset=offset)
                    )
                    
                    # Convert to pandas and then to dict
                    pandas_df = await loop.run_in_executor(pool, batch_df.to_pandas)
                    records = pandas_df.to_dict(orient="records")
                    
                    if not records:
                        break
                        
                    yield records
                    offset += batch_size
                
        except Exception as e:
            activity.logger.error(f"Error running query: {str(e)}")
            raise

        activity.logger.info("Query execution completed")