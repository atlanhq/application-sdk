"""
This example demonstrates how to use the Iceberg client and handler for data extraction.

It shows how to:
1. Configure and connect to a local Iceberg catalog
2. Perform a preflight check to verify table access
3. Extract data from an Iceberg table with filtering
4. Process the data in batches
5. Write extracted data to another Iceberg table (or optionally to JSON)

Usage:
1. Ensure you have a local Iceberg catalog at the specified location
2. Update the credentials to match your local environment
3. Run the script to extract data from the specified table
"""

import asyncio
import os
from typing import Any, Dict, List, Type, Optional

import pandas as pd
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SQLCatalog

from application_sdk.clients.iceberg import IcebergClient
from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.decorators import transform
from application_sdk.handlers.iceberg import IcebergHandler
from application_sdk.inputs.iceberg import IcebergInput
from application_sdk.outputs.iceberg import IcebergOutput
from application_sdk.outputs.json import JsonOutput
from application_sdk.worker import Worker
from application_sdk.workflows import WorkflowInterface

APPLICATION_NAME = "iceberg-extractor"

logger = get_logger(__name__)


class DataExtractionWorkflow(WorkflowInterface):
    """Simple workflow for data extraction."""
    
    @staticmethod
    def get_activities(activities: Any) -> List[Any]:
        """Get the activities for this workflow"""
        return [activities.extract_data]


class IcebergExtractorActivities:
    """Activities for extracting data from Iceberg tables."""

    def __init__(
        self, 
        iceberg_client_class: Type[IcebergClient] = IcebergClient, 
        iceberg_handler_class: Type[IcebergHandler] = IcebergHandler
    ):
        """Initialize the Iceberg extractor activities.

        Args:
            iceberg_client_class: The Iceberg client class to use.
            iceberg_handler_class: The Iceberg handler class to use.
        """
        self.iceberg_client_class = iceberg_client_class
        self.iceberg_handler_class = iceberg_handler_class
        self._workflow_state = {}

    @transform(
        table_data=IcebergInput(table=None),
        json_output=JsonOutput(output_suffix="/data")
    )
    async def extract_data(
        self,
        workflow_args: Dict[str, Any],
        table_data: pd.DataFrame = None,
        json_output: JsonOutput = None
    ) -> Dict[str, Any]:
        """Extract data from an Iceberg table.

        Args:
            workflow_args: Arguments for the workflow, including credentials and table info.
            table_data: DataFrame containing the extracted data (filled by decorator)
            json_output: Output for writing extracted data (filled by decorator)

        Returns:
            Dictionary with extraction results.
        """
        # Set up client and handler
        iceberg_client = self.iceberg_client_class()
        iceberg_handler = self.iceberg_handler_class(iceberg_client)

        try:
            # Load the client with credentials
            credentials = workflow_args.get("credentials", {})
            await iceberg_handler.load(credentials)

            # Run preflight check
            namespace = credentials.get("namespace")
            table_name = workflow_args.get("table")
            
            if not namespace or not table_name:
                raise ValueError("Namespace and table name are required")
                
            preflight_result = await iceberg_handler.preflight_check({
                "namespace": namespace,
                "table": table_name
            })

            logger.info(f"Preflight check successful: {preflight_result}")

            # Set up output paths
            output_path = workflow_args.get("output_path", "./iceberg_data")
            os.makedirs(output_path, exist_ok=True)

            # Extract data using the enhanced query functionality
            # Construct a query with filtering based on workflow arguments
            query = self._build_query_from_args(namespace, table_name, workflow_args)
            total_records = 0
            
            # If we're using the complex query format, we need to extract the table name
            table_identifier = query if isinstance(query, str) else query.get("table")
            
            logger.info(f"Running query: {query}")
            
            # Load the table first for metadata operations
            if "." in table_identifier:
                table_namespace, table_name = table_identifier.split(".", 1)
            else:
                table_namespace = namespace
                
            # Load the table
            table = iceberg_client.catalog.load_table(f"{table_namespace}.{table_name}")
            
            # Set up Iceberg output if specified
            write_to_iceberg = workflow_args.get("write_to_iceberg", False)
            if write_to_iceberg:
                # Set up the catalog for writing
                target_catalog_uri = credentials.get("catalog_uri")
                target_namespace = workflow_args.get("target_namespace", namespace)
                target_table = workflow_args.get("target_table", f"{table_name}_copy")
                
                # Create a catalog instance for the output
                catalog_config = {"uri": target_catalog_uri}
                if "warehouse_path" in credentials:
                    catalog_config["warehouse"] = credentials["warehouse_path"]
                
                target_catalog = SQLCatalog("iceberg_output", **catalog_config)
                
                # Create the output
                iceberg_output = IcebergOutput(
                    iceberg_catalog=target_catalog,
                    iceberg_namespace=target_namespace,
                    iceberg_table=target_table,
                    mode="overwrite"  # Can be configured through workflow_args
                )
                
                # Execute the query and process data
                all_records = []
                async for batch in iceberg_client.run_query(query):
                    all_records.extend(batch)
                    total_records += len(batch)
                    logger.info(f"Processed batch: {len(batch)} records, total: {total_records}")
                
                # Convert to pandas DataFrame and write to Iceberg
                if all_records:
                    df = pd.DataFrame(all_records)
                    await iceberg_output.write_dataframe(df)
                    logger.info(f"Data written to Iceberg table {target_namespace}.{target_table}, records: {total_records}")
            
            # Always write to JSON as a fallback
            if json_output:
                # The decorator will automatically load the table from the query
                # and pass the data to the json_output
                all_records = []
                async for batch in iceberg_client.run_query(query):
                    all_records.extend(batch)
                    total_records += len(batch)
                    logger.info(f"Processed batch: {len(batch)} records, total: {total_records}")
                
                if all_records:
                    df = pd.DataFrame(all_records)
                    await json_output.write_dataframe(df)
                    logger.info(f"Data written to JSON, records: {total_records}")

            logger.info(f"Data extraction complete. Total records: {total_records}")
            return {
                "status": "success",
                "table": table_identifier,
                "record_count": total_records,
                "output_path": output_path
            }

        except Exception as e:
            logger.error(f"Error extracting data: {str(e)}")
            return {
                "status": "error",
                "error": str(e)
            }
        finally:
            # Close the connection
            await iceberg_client.close()
            
    def _build_query_from_args(
        self,
        namespace: str,
        table: str,
        workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build a query dictionary from workflow arguments.
        
        Args:
            namespace: The namespace of the table.
            table: The table name.
            workflow_args: Workflow arguments containing query parameters.
            
        Returns:
            A query dictionary for the run_query method.
        """
        # Check if a complete query is provided in the workflow_args
        if "query" in workflow_args:
            return workflow_args["query"]
            
        # Get query parameters from workflow_args
        select_columns = workflow_args.get("select_columns")
        filters = workflow_args.get("filters")
        limit = workflow_args.get("limit")
        
        # If no advanced query parameters, return simple table name
        if not select_columns and not filters and not limit:
            return f"{namespace}.{table}"
            
        # Build query dictionary
        query = {
            "table": f"{namespace}.{table}"
        }
        
        if select_columns:
            query["select"] = select_columns
            
        if filters:
            query["where"] = filters
            
        if limit:
            query["limit"] = limit
            
        return query


async def application_iceberg(daemon: bool = True) -> Dict[str, Any]:
    """Run the Iceberg application.

    Args:
        daemon: Whether to run the worker as a daemon.

    Returns:
        Dictionary with workflow response.
    """
    print("Starting application_iceberg")

    # Create a temporal client
    temporal_client = TemporalClient(
        application_name=APPLICATION_NAME,
    )
    await temporal_client.load()

    # Create activities
    activities = IcebergExtractorActivities()

    # Create worker
    worker = Worker(
        temporal_client=temporal_client,
        workflow_classes=[DataExtractionWorkflow],
        activities=[activities.extract_data],
    )

    # Define workflow arguments with query filtering
    workflow_args = {
        "credentials": {
            "catalog_uri": "sqlite:///samplelake/catalog.db",  # Local SQLite catalog
            "warehouse_path": "file:///path/to/samplelake",    # Local warehouse path
            "namespace": "lake",                              # Default namespace
        },
        "table": "entities",                                  # Table to extract
        "output_path": "./extracted_data",                    # Output path
        "write_to_iceberg": True,                             # Whether to write to Iceberg
        "target_namespace": "lake",                           # Target namespace for writing
        "target_table": "entities_copy",                      # Target table for writing
        
        # Advanced query parameters (optional)
        "select_columns": ["name", "qualifiedName", "type"],  # Only select specific columns
        "filters": {                                          # Apply filters to the query
            "field": "type",
            "op": "=",
            "value": "table"
        },
        "limit": 1000                                         # Limit the number of results
    }

    # Example of a more complex query with multiple filters
    complex_query_example = {
        "query": {
            "table": "lake.entities",
            "select": ["name", "qualifiedName", "description"],
            "where": {
                "and": [
                    {"field": "type", "op": "=", "value": "table"},
                    {"field": "name", "op": "starts with", "value": "customer"}
                ]
            },
            "limit": 500
        }
    }
    
    # Uncomment to use the complex query example
    # workflow_args.update(complex_query_example)

    # Start the workflow
    workflow_response = await temporal_client.start_workflow(
        workflow_args, DataExtractionWorkflow
    )

    # Start the worker
    await worker.start(daemon=daemon)

    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_iceberg()) 