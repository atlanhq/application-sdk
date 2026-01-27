import os
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
    overload,
)

from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common import sql_utils
from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import (
    auto_heartbeater,
    get_object_store_prefix,
    get_workflow_id,
)
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.common.error_codes import ActivityError
from application_sdk.common.utils import prepare_query, read_sql_files
from application_sdk.constants import APP_TENANT_ID, APPLICATION_NAME, SQL_QUERIES_PATH
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.io import DataframeType
from application_sdk.io.json import JsonFileWriter
from application_sdk.io.parquet import ParquetFileReader, ParquetFileWriter
from application_sdk.io.utils import is_empty_dataframe
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.atlan_storage import AtlanStorage
from application_sdk.services.secretstore import SecretStore
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.query import QueryBasedTransformer

logger = get_logger(__name__)
activity.logger = logger

queries = read_sql_files(queries_prefix=SQL_QUERIES_PATH)

if TYPE_CHECKING:
    import pandas as pd


class BaseSQLMetadataExtractionActivitiesState(ActivitiesState):
    """State class for SQL metadata extraction activities.

    This class holds the state required for SQL metadata extraction activities,
    including the SQL client, handler, and transformer instances.

    Attributes:
        sql_client (BaseSQLClient): Client for SQL database operations.
        handler (BaseSQLHandler): Handler for SQL-specific operations.
        transformer (TransformerInterface): Transformer for metadata conversion.
    """

    sql_client: Optional[BaseSQLClient] = None
    handler: Optional[BaseSQLHandler] = None
    transformer: Optional[TransformerInterface] = None
    last_updated_timestamp: Optional[datetime] = None


class BaseSQLMetadataExtractionActivities(ActivitiesInterface):
    """Activities for extracting metadata from SQL databases.

    This class provides activities for extracting metadata from SQL databases,
    including databases, schemas, tables, and columns. It supports customization
    of the SQL client, handler, and transformer classes.

    Attributes:
        fetch_database_sql (Optional[str]): SQL query for fetching databases.
        fetch_schema_sql (Optional[str]): SQL query for fetching schemas.
        fetch_table_sql (Optional[str]): SQL query for fetching tables.
        fetch_column_sql (Optional[str]): SQL query for fetching columns.
        sql_client_class (Type[BaseSQLClient]): Class for SQL client operations.
        handler_class (Type[BaseSQLHandler]): Class for SQL handling operations.
        transformer_class (Type[TransformerInterface]): Class for metadata transformation.
        extract_temp_table_regex_table_sql (str): SQL snippet for excluding temporary tables during tables extraction.
            Defaults to an empty string.
        extract_temp_table_regex_column_sql (str): SQL snippet for excluding temporary tables during column extraction.
            Defaults to an empty string.
    """

    _state: Dict[str, BaseSQLMetadataExtractionActivitiesState] = {}

    fetch_database_sql = queries.get("EXTRACT_DATABASE")
    fetch_schema_sql = queries.get("EXTRACT_SCHEMA")
    fetch_table_sql = queries.get("EXTRACT_TABLE")
    fetch_column_sql = queries.get("EXTRACT_COLUMN")
    fetch_procedure_sql = queries.get("EXTRACT_PROCEDURE")

    extract_temp_table_regex_table_sql = queries.get("EXTRACT_TEMP_TABLE_REGEX_TABLE")
    extract_temp_table_regex_column_sql = queries.get("EXTRACT_TEMP_TABLE_REGEX_COLUMN")

    sql_client_class: Type[BaseSQLClient] = BaseSQLClient
    handler_class: Type[BaseSQLHandler] = BaseSQLHandler
    transformer_class: Type[TransformerInterface] = QueryBasedTransformer

    def __init__(
        self,
        sql_client_class: Optional[Type[BaseSQLClient]] = None,
        handler_class: Optional[Type[BaseSQLHandler]] = None,
        transformer_class: Optional[Type[TransformerInterface]] = None,
        multidb: bool = False,
    ):
        """Initialize the SQL metadata extraction activities.

        Args:
            sql_client_class (Type[BaseSQLClient], optional): Class for SQL client operations.
                Defaults to BaseSQLClient.
            handler_class (Type[BaseSQLHandler], optional): Class for SQL handling operations.
                Defaults to BaseSQLHandler.
            transformer_class (Type[TransformerInterface], optional): Class for metadata transformation.
                Defaults to QueryBasedTransformer.
            multidb (bool): When True, executes queries across multiple databases using
                `multidb_query_executor`. Defaults to False.
        """
        if sql_client_class:
            self.sql_client_class = sql_client_class
        if handler_class:
            self.handler_class = handler_class
        if transformer_class:
            self.transformer_class = transformer_class

        # Control whether to execute per-db using multidb executor
        self.multidb = multidb

        super().__init__()

    # State methods
    async def _get_state(self, workflow_args: Dict[str, Any]):
        """Gets the current state for the workflow.

        Args:
            workflow_args (Dict[str, Any]): Arguments passed to the workflow.

        Returns:
            BaseSQLMetadataExtractionActivitiesState: The current state.
        """
        return await super()._get_state(workflow_args)

    async def _set_state(self, workflow_args: Dict[str, Any]):
        """Sets up the state for the workflow.

        This method initializes the SQL client, handler, and transformer based on
        the workflow arguments.

        Args:
            workflow_args (Dict[str, Any]): Arguments passed to the workflow.

        Note:
            This method creates and configures the new SQL client before closing
            the old one to ensure state is never left with a closed client if
            initialization fails. The timestamp is only updated after the new
            client is successfully created and assigned.
        """
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = BaseSQLMetadataExtractionActivitiesState()

        existing_state = self._state[workflow_id]

        # Update workflow_args early, but preserve old timestamp until new client is ready
        # This ensures that if initialization fails, the state can still be refreshed
        existing_state.workflow_args = workflow_args

        # Store reference to old client for cleanup after new client is ready
        old_sql_client = None
        if existing_state and existing_state.sql_client is not None:
            old_sql_client = existing_state.sql_client

        # Create and configure new client BEFORE closing old one
        # This ensures state is never left with a closed client if initialization fails
        sql_client = self.sql_client_class()

        # Load credentials BEFORE creating handler to avoid race condition
        if "credential_guid" in workflow_args:
            credentials = await SecretStore.get_credentials(
                workflow_args["credential_guid"]
            )
            await sql_client.load(credentials)

        # Only after new client is successfully created and configured,
        # close old client and assign new one to state
        if old_sql_client is not None:
            try:
                await old_sql_client.close()
                logger.debug(
                    f"Closed existing SQL client for workflow {workflow_id} during state refresh"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to close existing SQL client for workflow {workflow_id}: {e}",
                    exc_info=True,
                )
                # Continue even if close fails - new client is already ready

        # Assign sql_client and handler to state AFTER new client is ready
        self._state[workflow_id].sql_client = sql_client
        handler = self.handler_class(sql_client)
        self._state[workflow_id].handler = handler
        # Update timestamp only after successful client creation and assignment
        # This ensures that if initialization fails, the old timestamp remains
        # and the state can be refreshed again immediately
        self._state[workflow_id].last_updated_timestamp = datetime.now()

        # Create transformer with required parameters from ApplicationConstants
        transformer_params = {
            "connector_name": APPLICATION_NAME,
            "connector_type": "sql",
            "tenant_id": APP_TENANT_ID,
        }
        self._state[workflow_id].transformer = self.transformer_class(
            **transformer_params
        )

    async def _clean_state(self):
        """Cleans up the state after workflow completion.

        This method ensures proper cleanup of resources, particularly closing
        the SQL client connection.
        """
        try:
            workflow_id = get_workflow_id()
            state = self._state.get(workflow_id)
            if state and state.sql_client is not None:
                await state.sql_client.close()
        except Exception as e:
            logger.warning("Failed to close SQL client", exc_info=e)

        await super()._clean_state()

    def _validate_output_args(
        self, workflow_args: Dict[str, Any]
    ) -> Tuple[str, str, str, str, str]:
        """Validates output prefix and path arguments.

        Args:
            workflow_args: Arguments passed to the workflow.

        Returns:
            Tuple containing output_prefix and output_path.

        Raises:
            ValueError: If output_prefix or output_path is not provided.
        """
        output_prefix = workflow_args.get("output_prefix")
        output_path = workflow_args.get("output_path")
        typename = workflow_args.get("typename")
        workflow_id = workflow_args.get("workflow_id")
        workflow_run_id = workflow_args.get("workflow_run_id")
        if (
            not output_prefix
            or not output_path
            or not typename
            or not workflow_id
            or not workflow_run_id
        ):
            logger.warning("Missing required workflow arguments")
            raise ValueError("Missing required workflow arguments")
        return output_prefix, output_path, typename, workflow_id, workflow_run_id

    @overload
    async def query_executor(
        self,
        sql_client: BaseSQLClient,
        sql_query: Optional[str],
        workflow_args: Dict[str, Any],
        output_path: str,
        typename: str,
        write_to_file: bool = True,
        concatenate: bool = False,
        return_dataframe: bool = False,
    ) -> Optional[ActivityStatistics]: ...

    @overload
    async def query_executor(
        self,
        sql_client: BaseSQLClient,
        sql_query: Optional[str],
        workflow_args: Dict[str, Any],
        output_path: str,
        typename: str,
        write_to_file: bool = True,
        concatenate: bool = False,
        return_dataframe: bool = True,
    ) -> Optional[Union[ActivityStatistics, "pd.DataFrame"]]: ...

    async def query_executor(
        self,
        sql_client: BaseSQLClient,
        sql_query: Optional[str],
        workflow_args: Dict[str, Any],
        output_path: str,
        typename: str,
        write_to_file: bool = True,
        concatenate: bool = False,
        return_dataframe: bool = False,
    ) -> Optional[Union[ActivityStatistics, "pd.DataFrame"]]:
        """
        Executes a SQL query using the provided client and saves the results to Parquet.

        This method validates the input client and query, prepares the query using
        workflow arguments, executes it, writes the resulting DataFrame to
        a Parquet file, and returns statistics about the output.

        Args:
            sql_client: The SQL client instance to use for executing the query.
            sql_query: The SQL query string to execute. Placeholders can be used which
                   will be replaced using `workflow_args`.
            workflow_args: Dictionary containing arguments for the workflow.
            output_path: Full path where the output files will be written.
            typename: Type name used for generating output statistics.
            write_to_file: Whether to write results to file. Defaults to True.
            concatenate: Whether to concatenate results in multidb mode. Defaults to False.
            return_dataframe: Whether to return a DataFrame instead of statistics. Defaults to False.

        Returns:
            Optional[Union[ActivityStatistics, pd.DataFrame]]: Statistics about the generated Parquet file,
            or a DataFrame if return_dataframe=True, or None if the query is empty or execution fails.

        Raises:
            ValueError: If `sql_client` is not provided.
        """
        # Common pre-checks and setup shared by both multidb and single-db paths
        if not sql_client:
            logger.error("SQL client is not provided")
            raise ValueError("SQL client is required for query execution")

        if not sql_query:
            logger.warning("Query is empty, skipping execution.")
            return None

        # Setup parquet output using helper method
        parquet_output = self._setup_parquet_output(
            output_path, write_to_file, typename
        )

        # If multidb mode is enabled, run per-database flow
        if getattr(self, "multidb", False):
            return await sql_utils.execute_multidb_flow(
                sql_client=sql_client,
                sql_query=sql_query,
                workflow_args=workflow_args,
                fetch_database_sql=self.fetch_database_sql,
                output_path=output_path,
                typename=typename,
                write_to_file=write_to_file,
                concatenate=concatenate,
                return_dataframe=return_dataframe,
                parquet_output=parquet_output,
                temp_table_regex_sql=self._get_temp_table_regex_sql(typename),
                setup_parquet_output_func=self._setup_parquet_output,
            )

        # Single-db execution path
        # Prepare query for single-db execution
        prepared_query = sql_utils.prepare_database_query(
            sql_query,
            None,
            workflow_args,
            self._get_temp_table_regex_sql(typename),
        )

        # Execute using helper method
        success, _ = await sql_utils.execute_single_db(
            sql_client, prepared_query, parquet_output, write_to_file
        )

        if not success:
            logger.error("Failed to execute single-db query")
            return None

        if parquet_output:
            logger.info(
                f"Successfully wrote query results to {parquet_output.get_full_path()}"
            )
            return await parquet_output.close()

        logger.warning("No parquet output configured for single-db execution")
        return None

    def _setup_parquet_output(
        self,
        output_path: str,
        write_to_file: bool,
        typename: Optional[str] = None,
    ) -> Optional[ParquetFileWriter]:
        """Create a ParquetFileWriter for the given output path.

        Args:
            output_path: Full path where the output files will be written.
            write_to_file: Whether to write results to file.

        Returns:
            Optional[ParquetFileWriter]: A ParquetFileWriter instance, or None if write_to_file is False.
        """
        if not write_to_file:
            return None

        return ParquetFileWriter(
            path=output_path,
            use_consolidation=True,
            typename=typename,
        )

    def _get_temp_table_regex_sql(self, typename: str) -> str:
        """Get the appropriate temp table regex SQL based on typename."""
        if typename == "column":
            return self.extract_temp_table_regex_column_sql or ""
        elif typename == "table":
            return self.extract_temp_table_regex_table_sql or ""
        else:
            return ""

    @activity.defn
    @auto_heartbeater
    async def fetch_databases(
        self, workflow_args: Dict[str, Any]
    ) -> Optional[ActivityStatistics]:
        """Fetch databases from the source database.

        Args:
            workflow_args: Dictionary containing arguments for the workflow.

        Returns:
            Optional[ActivityStatistics]: Statistics about the extracted databases.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client:
            logger.error("SQL client not initialized")
            raise ValueError("SQL client not initialized")

        prepared_query = prepare_query(
            query=self.fetch_database_sql, workflow_args=workflow_args
        )
        base_output_path = workflow_args.get("output_path", "")
        statistics = await self.query_executor(
            sql_client=state.sql_client,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_path=os.path.join(base_output_path, "raw"),
            typename="database",
        )
        return statistics

    @activity.defn
    @auto_heartbeater
    async def fetch_schemas(
        self, workflow_args: Dict[str, Any]
    ) -> Optional[ActivityStatistics]:
        """Fetch schemas from the source database.

        Args:
            workflow_args: Dictionary containing arguments for the workflow.

        Returns:
            Optional[ActivityStatistics]: Statistics about the extracted schemas.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client:
            logger.error("SQL client not initialized")
            raise ValueError("SQL client not initialized")

        prepared_query = prepare_query(
            query=self.fetch_schema_sql, workflow_args=workflow_args
        )
        base_output_path = workflow_args.get("output_path", "")
        statistics = await self.query_executor(
            sql_client=state.sql_client,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_path=os.path.join(base_output_path, "raw"),
            typename="schema",
        )
        return statistics

    @activity.defn
    @auto_heartbeater
    async def fetch_tables(
        self, workflow_args: Dict[str, Any]
    ) -> Optional[ActivityStatistics]:
        """Fetch tables from the source database.

        Args:
            workflow_args: Dictionary containing arguments for the workflow.

        Returns:
            Optional[ActivityStatistics]: Statistics about the extracted tables, or None if extraction failed.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client:
            logger.error("SQL client not initialized")
            raise ValueError("SQL client not initialized")

        prepared_query = prepare_query(
            query=self.fetch_table_sql,
            workflow_args=workflow_args,
            temp_table_regex_sql=self.extract_temp_table_regex_table_sql,
        )
        base_output_path = workflow_args.get("output_path", "")
        statistics = await self.query_executor(
            sql_client=state.sql_client,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_path=os.path.join(base_output_path, "raw"),
            typename="table",
        )
        return statistics

    @activity.defn
    @auto_heartbeater
    async def fetch_columns(
        self, workflow_args: Dict[str, Any]
    ) -> Optional[ActivityStatistics]:
        """Fetch columns from the source database.

        Args:
            workflow_args: Dictionary containing arguments for the workflow.

        Returns:
            Optional[ActivityStatistics]: Statistics about the extracted columns, or None if extraction failed.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client:
            logger.error("SQL client not initialized")
            raise ValueError("SQL client not initialized")

        prepared_query = prepare_query(
            query=self.fetch_column_sql,
            workflow_args=workflow_args,
            temp_table_regex_sql=self.extract_temp_table_regex_column_sql,
        )
        base_output_path = workflow_args.get("output_path", "")
        statistics = await self.query_executor(
            sql_client=state.sql_client,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_path=os.path.join(base_output_path, "raw"),
            typename="column",
        )
        return statistics

    @activity.defn
    @auto_heartbeater
    async def fetch_procedures(
        self, workflow_args: Dict[str, Any]
    ) -> Optional[ActivityStatistics]:
        """Fetch procedures from the source database.

        Args:
            workflow_args: Dictionary containing arguments for the workflow.

        Returns:
            Optional[ActivityStatistics]: Statistics about the extracted procedures, or None if extraction failed.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client:
            logger.error("SQL client not initialized")
            raise ValueError("SQL client not initialized")

        prepared_query = prepare_query(
            query=self.fetch_procedure_sql, workflow_args=workflow_args
        )
        base_output_path = workflow_args.get("output_path", "")
        statistics = await self.query_executor(
            sql_client=state.sql_client,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_path=os.path.join(base_output_path, "raw"),
            typename="extras-procedure",
        )
        return statistics

    @activity.defn
    @auto_heartbeater
    async def transform_data(
        self,
        workflow_args: Dict[str, Any],
    ) -> ActivityStatistics:
        """Transforms raw data into the required format.

        Args:
            raw_input (Any): Input data to transform.
            transformed_output (JsonFileWriter): Output handler for transformed data.
            **kwargs: Additional keyword arguments.

        Returns:
            ActivityStatistics: Statistics about the transformed data, including:
                - total_record_count: Total number of records processed
                - chunk_count: Number of chunks processed
                - typename: Type of data processed
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        output_prefix, output_path, typename, workflow_id, workflow_run_id = (
            self._validate_output_args(workflow_args)
        )

        raw_input = ParquetFileReader(
            path=os.path.join(output_path, "raw"),
            file_names=workflow_args.get("file_names"),
            dataframe_type=DataframeType.daft,
        )
        raw_input = raw_input.read_batches()

        transformed_output = JsonFileWriter(
            path=os.path.join(output_path, "transformed"),
            typename=typename,
            chunk_start=workflow_args.get("chunk_start"),
            dataframe_type=DataframeType.daft,
        )
        if state.transformer:
            workflow_args["connection_name"] = workflow_args.get("connection", {}).get(
                "connection_name", None
            )
            workflow_args["connection_qualified_name"] = workflow_args.get(
                "connection", {}
            ).get("connection_qualified_name", None)

            async for dataframe in raw_input:
                if not is_empty_dataframe(dataframe):
                    transform_metadata = state.transformer.transform_metadata(
                        dataframe=dataframe, **workflow_args
                    )
                    await transformed_output.write(transform_metadata)
        return await transformed_output.close()

    @activity.defn
    @auto_heartbeater
    async def upload_to_atlan(
        self, workflow_args: Dict[str, Any]
    ) -> ActivityStatistics:
        """Upload transformed data to Atlan storage.

        This activity uploads the transformed data from object store to Atlan storage
        (S3 via Dapr). It only runs if ENABLE_ATLAN_UPLOAD is set to true and the
        Atlan storage component is available.

        Args:
            workflow_args (Dict[str, Any]): Workflow configuration containing paths and metadata.

        Returns:
            ActivityStatistics: Upload statistics or skip statistics if upload is disabled.

        Raises:
            ValueError: If workflow_id or workflow_run_id are missing.
            ActivityError: If the upload fails with any migration errors when ENABLE_ATLAN_UPLOAD is true.
        """

        # Upload data from object store to Atlan storage
        # Use workflow_id/workflow_run_id as the prefix to migrate specific data
        migration_prefix = get_object_store_prefix(workflow_args["output_path"])
        logger.info(
            f"Starting migration from object store with prefix: {migration_prefix}"
        )
        upload_stats = await AtlanStorage.migrate_from_objectstore_to_atlan(
            prefix=migration_prefix
        )

        # Log upload statistics
        logger.info(
            f"Atlan upload completed: {upload_stats.migrated_files} files uploaded, "
            f"{upload_stats.failed_migrations} failed"
        )

        if upload_stats.failures:
            logger.error(f"Upload failed with {len(upload_stats.failures)} errors")
            for failure in upload_stats.failures:
                logger.error(f"Upload error: {failure}")

            # Mark activity as failed when there are upload failures
            raise ActivityError(
                f"{ActivityError.ATLAN_UPLOAD_ERROR}: Atlan upload failed with {len(upload_stats.failures)} errors. "
                f"Failed migrations: {upload_stats.failed_migrations}, "
                f"Total files: {upload_stats.total_files}"
            )

        return ActivityStatistics(
            total_record_count=upload_stats.migrated_files,
            chunk_count=upload_stats.total_files,
            typename="atlan-upload-completed",
        )
