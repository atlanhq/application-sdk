"""
Iceberg client implementation for Iceberg catalog connections.

This module provides client classes for connecting to and extracting data from
Iceberg tables using the Daft engine.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Union

from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SQLCatalog
from pyiceberg.expressions import Expression
from pyiceberg.table import Table
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
        """Initialize the Iceberg client.

        Args:
            catalog_uri (Optional[str], optional): URI for connecting to the Iceberg catalog.
                Defaults to None.
            warehouse_path (Optional[str], optional): Path to the Iceberg warehouse.
                Defaults to None.
            namespace (Optional[str], optional): Iceberg namespace. Defaults to None.
            credentials (Dict[str, Any], optional): Connection credentials. Defaults to {}.
        """
        self.catalog_uri = catalog_uri
        self.warehouse_path = warehouse_path
        self.namespace = namespace
        self.credentials = credentials

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load and establish the Iceberg catalog connection.

        Args:
            credentials (Dict[str, Any]): Connection credentials.

        Raises:
            ValueError: If connection fails due to authentication or connection issues
        """
        self.credentials = credentials
        try:
            self.catalog_uri = credentials.get("catalog_uri", self.catalog_uri)
            self.warehouse_path = credentials.get("warehouse_path", self.warehouse_path)
            self.namespace = credentials.get("namespace", self.namespace)
            
            catalog_config = self.get_catalog_connection()
            self.catalog = SQLCatalog("iceberg", **catalog_config)
            
            # Validate connection by trying to list tables
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
        """Get the catalog connection configuration.

        Returns:
            Dict[str, Any]: Configuration dictionary for connecting to the catalog.
        """
        if not self.catalog_uri:
            raise ValueError("Catalog URI is required")
        
        config = {
            "uri": self.catalog_uri,
        }
        
        if self.warehouse_path:
            config["warehouse"] = self.warehouse_path
            
        return config

    async def run_query(
        self, 
        query: Union[str, Dict[str, Any]], 
        batch_size: int = 100000
    ):
        """
        Run a query to extract data from Iceberg tables with optional filtering.
        
        Supports two query formats:
        1. Simple string format: "namespace.table_name" or "table_name"
        2. Dictionary format with filtering:
           {
               "table": "namespace.table_name" or "table_name",
               "select": ["column1", "column2"],  # Optional, defaults to all columns
               "where": {"field": "column_name", "op": "=", "value": "filter_value"},  # Optional
               "limit": 1000  # Optional, applies a limit to results
           }
        
        For complex filtering, the "where" can be a nested structure following PyIceberg's expression format.

        Args:
            query (Union[str, Dict[str, Any]]): Either a table identifier string or a query dictionary 
                                              with filtering and projection options
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

        try:
            # Parse the query
            table_identifier = None
            select_columns = None
            filter_expr = None
            row_limit = None
            
            if isinstance(query, str):
                # Simple format: "namespace.table" or just "table"
                table_identifier = query
            elif isinstance(query, dict):
                # Dictionary format with filters
                table_identifier = query.get("table")
                select_columns = query.get("select")
                filter_expr = query.get("where")
                row_limit = query.get("limit")
                
                if not table_identifier:
                    raise ValueError("Table identifier is required in query dictionary")
            else:
                raise ValueError("Query must be either a string or a dictionary")
                
            activity.logger.info(f"Running query on table: {table_identifier}")
            
            # Parse the table identifier to get namespace and table name
            if "." in table_identifier:
                namespace, table_name = table_identifier.split(".", 1)
            else:
                if not self.namespace:
                    raise ValueError("Namespace is required when table name doesn't include it")
                namespace = self.namespace
                table_name = table_identifier
                
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
                
                # Create a daft dataframe from the table, applying filters if specified
                if filter_expr:
                    # Convert the filter expression to PyIceberg format
                    iceberg_filter = self._create_filter_expression(filter_expr)
                    
                    # Read with filter
                    df = await loop.run_in_executor(
                        pool,
                        lambda: daft.read_iceberg(table, filter=iceberg_filter)
                    )
                else:
                    # Read without filter
                    df = await loop.run_in_executor(
                        pool,
                        lambda: daft.read_iceberg(table)
                    )
                
                # Apply column selection if specified
                if select_columns:
                    df = await loop.run_in_executor(
                        pool,
                        lambda: df.select(select_columns)
                    )
                
                # Apply limit if specified
                if row_limit:
                    df = await loop.run_in_executor(
                        pool,
                        lambda: df.limit(row_limit)
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
        
    def _create_filter_expression(self, filter_spec: Dict[str, Any]) -> Expression:
        """Convert a filter specification to a PyIceberg expression.
        
        Basic format:
        {"field": "column_name", "op": "=", "value": "filter_value"}
        
        Combined filters:
        {"and": [
            {"field": "column1", "op": "=", "value": "value1"},
            {"field": "column2", "op": ">", "value": 100}
        ]}
        
        Args:
            filter_spec (Dict[str, Any]): Filter specification dictionary
            
        Returns:
            Expression: PyIceberg filter expression
        """
        from pyiceberg.expressions import (
            And, Or, Not, Equal, NotEqual, GreaterThan, 
            GreaterThanOrEqual, LessThan, LessThanOrEqual, 
            In, NotIn, IsNull, NotNull, StartsWith, NotStartsWith
        )
        
        # Handle logical operators
        if "and" in filter_spec:
            subexpressions = [self._create_filter_expression(subfilter) for subfilter in filter_spec["and"]]
            return And(*subexpressions)
        elif "or" in filter_spec:
            subexpressions = [self._create_filter_expression(subfilter) for subfilter in filter_spec["or"]]
            return Or(*subexpressions)
        elif "not" in filter_spec:
            return Not(self._create_filter_expression(filter_spec["not"]))
        
        # Handle basic comparison
        field = filter_spec.get("field")
        op = filter_spec.get("op")
        value = filter_spec.get("value")
        
        if not field or not op:
            raise ValueError(f"Invalid filter specification: {filter_spec}")
        
        # Map operators to PyIceberg expressions
        if op == "=":
            return Equal(field, value)
        elif op == "!=":
            return NotEqual(field, value)
        elif op == ">":
            return GreaterThan(field, value)
        elif op == ">=":
            return GreaterThanOrEqual(field, value)
        elif op == "<":
            return LessThan(field, value)
        elif op == "<=":
            return LessThanOrEqual(field, value)
        elif op == "in":
            return In(field, value)
        elif op == "not in":
            return NotIn(field, value)
        elif op == "is null":
            return IsNull(field)
        elif op == "is not null":
            return NotNull(field)
        elif op == "starts with":
            return StartsWith(field, value)
        elif op == "not starts with":
            return NotStartsWith(field, value)
        else:
            raise ValueError(f"Unsupported operator: {op}") 