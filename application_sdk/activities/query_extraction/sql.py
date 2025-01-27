import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field
from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.utils import auto_heartbeater, get_workflow_id
from application_sdk.clients.sql import SQLClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.decorators import transform
from application_sdk.handlers.sql import SQLHandler
from application_sdk.inputs.objectstore import ObjectStore
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.inputs.statestore import StateStore
from application_sdk.outputs.json import JsonOutput

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class MinerArgs(BaseModel):
    """Arguments for SQL query mining operations.

    This class defines the configuration parameters needed for mining SQL queries
    from a database, including time ranges, chunk sizes, and SQL replacements.

    Attributes:
        database_name_cleaned (str): Cleaned name of the target database.
        schema_name_cleaned (str): Cleaned name of the target schema.
        timestamp_column (str): Name of the column containing timestamps.
        chunk_size (int): Number of records to process in each chunk.
        current_marker (int): Current timestamp marker for processing.
        sql_replace_from (str): Original SQL fragment to be replaced.
        sql_replace_to (str): Replacement SQL fragment with placeholders.
        ranged_sql_start_key (str): Placeholder for range start timestamp.
        ranged_sql_end_key (str): Placeholder for range end timestamp.
        miner_start_time_epoch (int): Start time for mining in epoch format.
            Defaults to 14 days ago.
    """

    database_name_cleaned: str
    schema_name_cleaned: str
    timestamp_column: str
    chunk_size: int
    current_marker: int
    sql_replace_from: str
    sql_replace_to: str
    ranged_sql_start_key: str
    ranged_sql_end_key: str
    miner_start_time_epoch: int = Field(
        default_factory=lambda: int((datetime.now() - timedelta(days=14)).timestamp())
    )


class SQLQueryExtractionActivitiesState(ActivitiesState):
    """State model for SQL query extraction activities.

    This class holds the state required for SQL query extraction activities,
    including the SQL client and handler instances.

    Attributes:
        sql_client (SQLClient): Client for SQL database operations.
        handler (SQLHandler): Handler for SQL-specific operations.
        workflow_args (Dict[str, Any]): Arguments passed to the workflow.
    """

    sql_client: SQLClient
    handler: SQLHandler
    workflow_args: Dict[str, Any]


class SQLQueryExtractionActivities(ActivitiesInterface):
    """Activities for extracting SQL queries from databases.

    This class provides activities for extracting and processing SQL queries
    from databases, with support for chunking and parallel processing.

    Attributes:
        _state (Dict[str, StateModel]): Internal state storage.
        sql_client_class (Type[SQLClient]): Class for SQL client operations.
        handler_class (Type[SQLHandler]): Class for SQL handling operations.
        fetch_queries_sql (str): SQL query template for fetching queries.
    """

    _state: Dict[str, SQLQueryExtractionActivitiesState] = {}

    sql_client_class: Type[SQLClient] = SQLClient
    handler_class: Type[SQLHandler] = SQLHandler

    fetch_queries_sql: str

    def __init__(
        self,
        sql_client_class: Optional[Type[SQLClient]] = None,
        handler_class: Optional[Type[SQLHandler]] = None,
    ):
        """Initialize the SQL query extraction activities.

        Args:
            sql_client_class (Type[SQLClient], optional): Class for SQL client operations.
                Defaults to SQLClient.
            handler_class (Type[SQLHandler], optional): Class for SQL handling operations.
                Defaults to SQLHandler.
        """
        if sql_client_class:
            self.sql_client_class = sql_client_class
        if handler_class:
            self.handler_class = handler_class

        super().__init__()

    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        """Sets up the state for the workflow.

        This method initializes the SQL client and handler based on the workflow arguments.

        Args:
            workflow_args (Dict[str, Any]): Arguments passed to the workflow.
        """
        sql_client = self.sql_client_class()
        if "credential_guid" in workflow_args:
            credentials = StateStore.extract_credentials(
                workflow_args["credential_guid"]
            )
            await sql_client.load(credentials)

        handler = self.handler_class(sql_client)

        self._state[get_workflow_id()] = SQLQueryExtractionActivitiesState(
            sql_client=sql_client,
            handler=handler,
            workflow_args=workflow_args,
        )

    @activity.defn
    @auto_heartbeater
    @transform(
        batch_input=SQLQueryInput(query="fetch_queries_sql"),
        raw_output=JsonOutput(output_suffix="/raw/query"),
    )
    async def fetch_queries(
        self,
        batch_input,
        raw_output: JsonOutput,
        **kwargs,
    ):
        """Fetch and process queries from the database.

        Args:
            batch_input: Input DataFrame containing the queries
            raw_output: JsonOutput object for writing results
            **kwargs: Additional keyword arguments

        Returns:
            None
        """
        await raw_output.write_df(batch_input)

    async def parallelize_query(
        self,
        query: str,
        timestamp_column: str,
        chunk_size: int,
        current_marker: str,
        sql_ranged_replace_from: str,
        sql_ranged_replace_to: str,
        ranged_sql_start_key: str,
        ranged_sql_end_key: str,
        sql_client: SQLClient,
    ):
        """Processes a single chunk of the query, collecting timestamp ranges.

        Args:
            query: The SQL query to process
            timestamp_column: Column name containing the timestamp
            chunk_size: Number of records per chunk
            current_marker: Starting timestamp marker
            sql_ranged_replace_from: Original SQL fragment to replace
            sql_ranged_replace_to: SQL fragment with range placeholders
            ranged_sql_start_key: Placeholder for range start timestamp
            ranged_sql_end_key: Placeholder for range end timestamp
            sql_client: SQLClient instance for executing queries

        Returns:
            List[Dict[str, Any]]: List of chunked queries with their metadata

        Raises:
            ValueError: If chunk size is less than or equal to 0
        """
        if chunk_size <= 0:
            raise ValueError("Chunk size must be greater than 0")

        parallel_markers: List[Dict[str, Any]] = []

        marked_sql = query.replace(ranged_sql_start_key, current_marker)
        rewritten_query = f"WITH T AS ({marked_sql}) SELECT {timestamp_column} FROM T ORDER BY {timestamp_column} ASC"
        logger.info(f"Executing query: {rewritten_query}")

        chunk_start_marker = None
        chunk_end_marker = None
        record_count = 0
        last_marker = None

        async for result_batch in sql_client.run_query(rewritten_query):
            for row in result_batch:
                timestamp = row[timestamp_column.lower()]
                new_marker = str(int(timestamp.timestamp() * 1000))

                if last_marker == new_marker:
                    logger.info("Skipping duplicate start time")
                    record_count += 1
                    continue

                if not chunk_start_marker:
                    chunk_start_marker = new_marker
                chunk_end_marker = new_marker
                record_count += 1
                last_marker = new_marker

                if record_count >= chunk_size:
                    self._create_chunked_query(
                        query=query,
                        start_marker=chunk_start_marker,
                        end_marker=chunk_end_marker,
                        parallel_markers=parallel_markers,
                        record_count=record_count,
                        sql_ranged_replace_from=sql_ranged_replace_from,
                        sql_ranged_replace_to=sql_ranged_replace_to,
                        ranged_sql_start_key=ranged_sql_start_key,
                        ranged_sql_end_key=ranged_sql_end_key,
                    )
                    record_count = 0
                    chunk_start_marker = None
                    chunk_end_marker = None

        if record_count > 0:
            self._create_chunked_query(
                query=query,
                start_marker=chunk_start_marker,
                end_marker=chunk_end_marker,
                parallel_markers=parallel_markers,
                record_count=record_count,
                sql_ranged_replace_from=sql_ranged_replace_from,
                sql_ranged_replace_to=sql_ranged_replace_to,
                ranged_sql_start_key=ranged_sql_start_key,
                ranged_sql_end_key=ranged_sql_end_key,
            )

        logger.info(f"Parallelized queries into {len(parallel_markers)} chunks")

        return parallel_markers

    def _create_chunked_query(
        self,
        query: str,
        start_marker: str | None,
        end_marker: str | None,
        parallel_markers: List[Dict[str, Any]],
        record_count: int,
        sql_ranged_replace_from: str,
        sql_ranged_replace_to: str,
        ranged_sql_start_key: str,
        ranged_sql_end_key: str,
    ) -> None:
        """Creates a chunked query with the specified time range and adds it to parallel_markers.

        Args:
            query: The base SQL query
            start_marker: Start timestamp for the chunk
            end_marker: End timestamp for the chunk
            parallel_markers: List to store the chunked queries
            record_count: Number of records in this chunk
            sql_ranged_replace_from: Original SQL fragment to replace
            sql_ranged_replace_to: SQL fragment with range placeholders
            ranged_sql_start_key: Placeholder for range start timestamp
            ranged_sql_end_key: Placeholder for range end timestamp

        Returns:
            None
        """
        if not start_marker or not end_marker:
            return

        chunked_sql = query.replace(
            sql_ranged_replace_from,
            sql_ranged_replace_to.replace(ranged_sql_start_key, start_marker).replace(
                ranged_sql_end_key, end_marker
            ),
        )

        logger.info(
            f"Processed {record_count} records in chunk {len(parallel_markers)}, "
            f"with start marker {start_marker} and end marker {end_marker}"
        )
        logger.info(f"Chunked SQL: {chunked_sql}")

        parallel_markers.append(
            {
                "sql": chunked_sql,
                "start": start_marker,
                "end": end_marker,
                "count": record_count,
            }
        )

    @activity.defn
    @auto_heartbeater
    async def get_query_batches(
        self, workflow_args: Dict[str, Any], **kwargs: Any
    ) -> List[Dict[str, Any]]:
        """Gets batches of queries by parallelizing the main query.

        Args:
            workflow_args: Dictionary containing workflow configuration
            **kwargs: Additional keyword arguments

        Returns:
            List[Dict[str, Any]]: List of parallelized query batches

        Raises:
            Exception: If query parallelization fails
        """
        state: SQLQueryExtractionActivitiesState = await self._get_state(workflow_args)
        sql_client = state.sql_client

        miner_args = MinerArgs(**workflow_args.get("miner_args", {}))

        queries_sql_query = self.fetch_queries_sql.format(
            database_name_cleaned=miner_args.database_name_cleaned,
            schema_name_cleaned=miner_args.schema_name_cleaned,
            miner_start_time_epoch=miner_args.miner_start_time_epoch,
        )

        try:
            parallel_markers = await self.parallelize_query(
                query=queries_sql_query,
                timestamp_column=miner_args.timestamp_column,
                chunk_size=miner_args.chunk_size,
                current_marker=str(miner_args.current_marker),
                sql_ranged_replace_from=miner_args.sql_replace_from,
                sql_ranged_replace_to=miner_args.sql_replace_to,
                ranged_sql_start_key=miner_args.ranged_sql_start_key,
                ranged_sql_end_key=miner_args.ranged_sql_end_key,
                sql_client=sql_client,
            )
        except Exception as e:
            logger.error(f"Failed to parallelize queries: {e}")
            raise e

        logger.info(f"Parallelized queries into {len(parallel_markers)} chunks")

        # Write the results to a metadata file
        output_path = os.path.join(workflow_args["output_path"], "raw", "query")
        metadata_file_path = os.path.join(output_path, "metadata.json")
        os.makedirs(os.path.dirname(metadata_file_path), exist_ok=True)
        with open(metadata_file_path, "w") as f:
            f.write(json.dumps(parallel_markers))

        await ObjectStore.push_file_to_object_store(
            workflow_args["output_prefix"], metadata_file_path
        )

        return parallel_markers
