import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field
from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.utils import auto_heartbeater, get_workflow_id
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers import HandlerInterface
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.inputs.objectstore import ObjectStoreInput
from application_sdk.inputs.secretstore import SecretStoreInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.objectstore import ObjectStoreOutput
from application_sdk.outputs.parquet import ParquetOutput
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.atlas import AtlasTransformer

logger = get_logger(__name__)


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


class BaseSQLQueryExtractionActivitiesState(ActivitiesState):
    """State model for SQL query extraction activities.

    This class holds the state required for SQL query extraction activities,
    including the SQL client and handler instances.

    Attributes:
        sql_client (BaseSQLClient): Client for SQL database operations.
        handler (BaseSQLHandler): Handler for SQL-specific operations.
        workflow_args (Dict[str, Any]): Arguments passed to the workflow.
    """

    sql_client: BaseSQLClient
    handler: HandlerInterface


class SQLQueryExtractionActivities(ActivitiesInterface):
    """Activities for extracting SQL queries from databases.

    This class provides activities for extracting and processing SQL queries
    from databases, with support for chunking and parallel processing.

    Attributes:
        _state (Dict[str, StateModel]): Internal state storage.
        sql_client_class (Type[BaseSQLClient]): Class for SQL client operations.
        handler_class (Type[BaseSQLHandler]): Class for SQL handling operations.
        fetch_queries_sql (str): SQL query template for fetching queries.
    """

    _state: Dict[str, BaseSQLQueryExtractionActivitiesState] = {}

    sql_client_class: Type[BaseSQLClient] = BaseSQLClient
    handler_class: Type[BaseSQLHandler] = BaseSQLHandler
    transformer_class: Type[TransformerInterface] = AtlasTransformer

    fetch_queries_sql: str

    def __init__(
        self,
        sql_client_class: Optional[Type[BaseSQLClient]] = None,
        handler_class: Optional[Type[BaseSQLHandler]] = None,
        transformer_class: Optional[Type[TransformerInterface]] = None,
    ):
        """Initialize the SQL query extraction activities.

        Args:
            sql_client_class (Type[BaseSQLClient], optional): Class for SQL client operations.
                Defaults to BaseSQLClient.
            handler_class (Type[BaseSQLHandler], optional): Class for SQL handling operations.
                Defaults to BaseSQLHandler.
        """
        if sql_client_class:
            self.sql_client_class = sql_client_class
        if handler_class:
            self.handler_class = handler_class
        if transformer_class:
            self.transformer_class = transformer_class

        super().__init__()

    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        """Sets up the state for the workflow.

        This method initializes the SQL client and handler based on the workflow arguments.

        Args:
            workflow_args (Dict[str, Any]): Arguments passed to the workflow.
        """
        workflow_id = get_workflow_id()
        sql_client = self.sql_client_class()
        if "credential_guid" in workflow_args:
            credentials = SecretStoreInput.extract_credentials(
                workflow_args["credential_guid"]
            )
            await sql_client.load(credentials)

        handler = self.handler_class(sql_client)

        self._state[workflow_id] = BaseSQLQueryExtractionActivitiesState(
            sql_client=sql_client,
            handler=handler,
            workflow_args=workflow_args,
        )

    def get_formatted_query(self, query: str, workflow_args: Dict[str, Any]) -> str:
        """Formats the query with the workflow arguments.

        Args:
            query (str): The query to format.
            workflow_args (Dict[str, Any]): The workflow arguments.
        """
        miner_args = MinerArgs(**workflow_args.get("miner_args", {}))
        temp_query = query.format(
            miner_start_time_epoch=miner_args.miner_start_time_epoch,
            database_name_cleaned=miner_args.database_name_cleaned,
            schema_name_cleaned=miner_args.schema_name_cleaned,
            timestamp_column=miner_args.timestamp_column,
            chunk_size=miner_args.chunk_size,
            current_marker=miner_args.current_marker,
            sql_replace_from=miner_args.sql_replace_from,
            sql_replace_to=miner_args.sql_replace_to,
        )

        temp_query = temp_query.replace(
            miner_args.sql_replace_from, miner_args.sql_replace_to
        )

        temp_query = temp_query.replace(
            miner_args.ranged_sql_start_key, workflow_args["start_marker"]
        )
        temp_query = temp_query.replace(
            miner_args.ranged_sql_end_key, workflow_args["end_marker"]
        )
        return temp_query

    @activity.defn
    @auto_heartbeater
    async def fetch_queries(
        self,
        workflow_args: Dict[str, Any],
    ):
        """Fetch and process queries from the database.

        This activity fetches SQL queries from the database using the configured SQL client
        and processes them into a Parquet output format.

        Args:
            workflow_args (Dict[str, Any]): Dictionary containing workflow configuration including:
                - credential_guid (str, optional): GUID for accessing credentials
                - output_prefix (str): Prefix for output files
                - output_path (str): Path where output files will be stored

        Returns:
            None

        Raises:
            Exception: If query fetching or processing fails
        """

        try:
            state = await self._get_state(workflow_args)
            sql_input = SQLQueryInput(
                engine=state.sql_client.engine,
                query=self.get_formatted_query(self.fetch_queries_sql, workflow_args),
            )
            sql_input = await sql_input.get_daft_dataframe()

            raw_output = ParquetOutput(
                output_prefix=workflow_args["output_prefix"],
                output_path=workflow_args["output_path"],
                output_suffix="raw/query",
                chunk_size=workflow_args["miner_args"].get("chunk_size", 100000),
                start_marker=workflow_args["start_marker"],
                end_marker=workflow_args["end_marker"],
            )
            await raw_output.write_daft_dataframe(sql_input)

            logger.info(
                f"Query fetch completed, {raw_output.total_record_count} records processed",
            )

        except Exception as e:
            logger.error(
                "Query fetch failed %s",
                e,
                exc_info=True,
            )
            raise

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
        sql_client: BaseSQLClient,
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
            sql_client: BaseSQLClient instance for executing queries

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
        """Create a chunked SQL query for parallel processing.

        This method modifies the original SQL query to process a specific chunk of data
        based on the provided markers and SQL replacement patterns.

        Args:
            query (str): The original SQL query to be chunked
            start_marker (str | None): Starting marker for the query range
            end_marker (str | None): Ending marker for the query range
            parallel_markers (List[Dict[str, Any]]): List of marker dictionaries for parallel processing
            record_count (int): Total number of records to process
            sql_ranged_replace_from (str): Original SQL fragment to be replaced
            sql_ranged_replace_to (str): Replacement SQL fragment with placeholders
            ranged_sql_start_key (str): Placeholder key for range start
            ranged_sql_end_key (str): Placeholder key for range end

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

    async def write_marker(
        self, parallel_markers: List[Dict[str, Any]], workflow_args: Dict[str, Any]
    ):
        """Write the marker to the output path.

        This method writes the last marker from the parallelized query results to a marker file.
        The marker file is used to track the progress of query extraction and can be used to
        resume processing from where it left off in subsequent runs.

        Args:
            parallel_markers (List[Dict[str, Any]]): List of parallelized query markers containing
                metadata about each chunk including start, end, and count information.
            workflow_args (Dict[str, Any]): Dictionary containing workflow configuration including:
                - output_prefix (str): Prefix for output files
                - miner_args (Dict[str, Any]): Mining arguments containing crossover_marker_file_path

        Returns:
            None

        Raises:
            Exception: If marker file writing or object store upload fails
        """
        output_path = workflow_args["output_path"].rsplit("/", 2)[0]
        logger.info(f"Writing marker file to {output_path}")
        marker_file_path = os.path.join(output_path, "markerfile")

        if not parallel_markers:
            logger.warning("No parallel markers generated, skipping marker file write.")
            return

        # find the last marker from the parallel_markers
        last_marker = parallel_markers[-1]["end"]
        with open(marker_file_path, "w") as f:
            f.write(last_marker)

        logger.info(f"Last marker: {last_marker}")
        await ObjectStoreOutput.push_file_to_object_store(
            workflow_args["output_prefix"], marker_file_path
        )
        logger.info(f"Marker file written to {marker_file_path}")

    def read_marker(self, workflow_args: Dict[str, Any]) -> Optional[int]:
        """Read the marker from the output path.

        This method reads the current marker value from a marker file to determine the
        starting point for query extraction. The marker represents a timestamp that
        indicates where the previous extraction process left off.

        Args:
            workflow_args (Dict[str, Any]): Dictionary containing workflow configuration.
                Currently not used in the implementation but kept for interface consistency.

        Returns:
            Optional[int]: The marker value as an integer timestamp, or None if the marker
                file cannot be read or doesn't exist.

        Raises:
            Exception: If marker file reading fails (logged as warning, not re-raised)
        """
        try:
            output_path = workflow_args["output_path"].rsplit("/", 2)[0]
            marker_file_path = os.path.join(output_path, "markerfile")
            logger.info(f"Downloading marker file from {marker_file_path}")

            os.makedirs(workflow_args["output_prefix"], exist_ok=True)

            ObjectStoreInput.download_file_from_object_store(
                workflow_args["output_prefix"], marker_file_path
            )

            logger.info(f"Output prefix: {workflow_args['output_prefix']}")
            logger.info(f"Marker file downloaded to {marker_file_path}")
            if not os.path.exists(marker_file_path):
                logger.warning(f"Marker file does not exist at {marker_file_path}")
                return None
            with open(marker_file_path, "r") as f:
                current_marker = f.read()
            logger.info(f"Current marker: {current_marker}")
            return int(current_marker)
        except Exception as e:
            logger.warning(f"Failed to read marker: {e}")
            return None

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
        state: BaseSQLQueryExtractionActivitiesState = await self._get_state(
            workflow_args
        )
        sql_client = state.sql_client

        miner_args = MinerArgs(**workflow_args.get("miner_args", {}))

        current_marker = self.read_marker(workflow_args)
        if current_marker:
            miner_args.miner_start_time_epoch = current_marker

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
        metadata_file_path = os.path.join(output_path, "metadata.json.ignore")
        os.makedirs(os.path.dirname(metadata_file_path), exist_ok=True)
        with open(metadata_file_path, "w") as f:
            f.write(json.dumps(parallel_markers))

        await ObjectStoreOutput.push_file_to_object_store(
            workflow_args["output_prefix"], metadata_file_path
        )

        try:
            await self.write_marker(parallel_markers, workflow_args)
        except Exception as e:
            logger.warning(f"Failed to write marker file: {e}")

        return parallel_markers
