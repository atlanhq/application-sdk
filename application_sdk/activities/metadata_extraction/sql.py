import os
from typing import Any, Dict, Optional, Tuple, Type, cast

from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import auto_heartbeater, get_workflow_id
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.common.dataframe_utils import is_empty_dataframe
from application_sdk.common.utils import prepare_query, read_sql_files
from application_sdk.constants import APP_TENANT_ID, APPLICATION_NAME, SQL_QUERIES_PATH
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.inputs.parquet import ParquetInput
from application_sdk.inputs.secretstore import SecretStoreInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.json import JsonOutput
from application_sdk.outputs.parquet import ParquetOutput
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.query import QueryBasedTransformer

logger = get_logger(__name__)
activity.logger = logger

queries = read_sql_files(queries_prefix=SQL_QUERIES_PATH)


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
    ):
        """Initialize the SQL metadata extraction activities.

        Args:
            sql_client_class (Type[BaseSQLClient], optional): Class for SQL client operations.
                Defaults to BaseSQLClient.
            handler_class (Type[BaseSQLHandler], optional): Class for SQL handling operations.
                Defaults to BaseSQLHandler.
            transformer_class (Type[TransformerInterface], optional): Class for metadata transformation.
                Defaults to QueryBasedTransformer.
        """
        if sql_client_class:
            self.sql_client_class = sql_client_class
        if handler_class:
            self.handler_class = handler_class
        if transformer_class:
            self.transformer_class = transformer_class

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
        """
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = BaseSQLMetadataExtractionActivitiesState()

        await super()._set_state(workflow_args)

        sql_client = self.sql_client_class()

        handler = self.handler_class(sql_client)
        self._state[workflow_id].handler = handler

        if "credential_guid" in workflow_args:
            credentials = SecretStoreInput.extract_credentials(
                workflow_args["credential_guid"]
            )
            await sql_client.load(credentials)

        self._state[workflow_id].sql_client = sql_client

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

    async def query_executor(
        self,
        sql_engine: Any,
        sql_query: Optional[str],
        workflow_args: Dict[str, Any],
        output_suffix: str,
        typename: str,
    ) -> Optional[ActivityStatistics]:
        """
        Executes a SQL query using the provided engine and saves the results to Parquet.

        This method validates the input engine and query, prepares the query using
        workflow arguments, executes it, writes the resulting Daft DataFrame to
        a Parquet file, and returns statistics about the output.

        Args:
            sql_engine: The SQL engine instance to use for executing the query.
            sql_query: The SQL query string to execute. Placeholders can be used which
                   will be replaced using `workflow_args`.
            workflow_args: Dictionary containing arguments for the workflow, used for
                           preparing the query and defining output paths. Expected keys:
                           - "output_prefix": Prefix for the output path.
                           - "output_path": Base directory for the output.
            output_suffix: Suffix to append to the output file name.
            typename: Type name used for generating output statistics.

        Returns:
            Optional[ActivityStatistics]: Statistics about the generated Parquet file,
            or None if the query is empty or execution fails before writing output.

        Raises:
            ValueError: If `sql_engine` is not provided.
        """
        if not sql_engine:
            logger.error("SQL engine is not set.")
            raise ValueError("SQL engine must be provided.")
        if not sql_query:
            logger.warning("Query is empty, skipping execution.")
            return None

        try:
            sql_input = SQLQueryInput(engine=sql_engine, query=sql_query)
            dataframe = await sql_input.get_batched_dataframe()

            output_prefix = workflow_args.get("output_prefix")
            output_path = workflow_args.get("output_path")

            if not output_prefix or not output_path:
                logger.error("Output prefix or path not provided in workflow_args.")
                raise ValueError(
                    "Output prefix and path must be specified in workflow_args."
                )

            parquet_output = ParquetOutput(
                output_prefix=output_prefix,
                output_path=output_path,
                output_suffix=output_suffix,
            )
            await parquet_output.write_batched_dataframe(dataframe)
            logger.info(
                f"Successfully wrote query results to {parquet_output.get_full_path()}"
            )

            statistics = await parquet_output.get_statistics(typename=typename)
            return statistics
        except Exception as e:
            logger.error(
                f"Error during query execution or output writing: {e}", exc_info=True
            )
            raise

    @activity.defn
    @auto_heartbeater
    async def fetch_databases(
        self, workflow_args: Dict[str, Any]
    ) -> Optional[ActivityStatistics]:
        """Fetch databases from the source database.

        Args:
            batch_input: DataFrame containing the raw database data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client or not state.sql_client.engine:
            logger.error("SQL client or engine not initialized")
            raise ValueError("SQL client or engine not initialized")

        prepared_query = prepare_query(
            query=self.fetch_database_sql, workflow_args=workflow_args
        )
        statistics = await self.query_executor(
            sql_engine=state.sql_client.engine,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_suffix="raw/database",
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
            batch_input: DataFrame containing the raw schema data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client or not state.sql_client.engine:
            logger.error("SQL client or engine not initialized")
            raise ValueError("SQL client or engine not initialized")

        prepared_query = prepare_query(
            query=self.fetch_schema_sql, workflow_args=workflow_args
        )
        statistics = await self.query_executor(
            sql_engine=state.sql_client.engine,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_suffix="raw/schema",
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
            batch_input: DataFrame containing the raw table data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Optional[ActivityStatistics]: Statistics about the extracted tables, or None if extraction failed.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client or not state.sql_client.engine:
            logger.error("SQL client or engine not initialized")
            raise ValueError("SQL client or engine not initialized")

        prepared_query = prepare_query(
            query=self.fetch_table_sql,
            workflow_args=workflow_args,
            temp_table_regex_sql=self.extract_temp_table_regex_table_sql,
        )
        statistics = await self.query_executor(
            sql_engine=state.sql_client.engine,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_suffix="raw/table",
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
            batch_input: DataFrame containing the raw column data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Optional[ActivityStatistics]: Statistics about the extracted columns, or None if extraction failed.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client or not state.sql_client.engine:
            logger.error("SQL client or engine not initialized")
            raise ValueError("SQL client or engine not initialized")

        prepared_query = prepare_query(
            query=self.fetch_column_sql,
            workflow_args=workflow_args,
            temp_table_regex_sql=self.extract_temp_table_regex_column_sql,
        )
        statistics = await self.query_executor(
            sql_engine=state.sql_client.engine,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_suffix="raw/column",
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
            batch_input: DataFrame containing the raw column data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Optional[ActivityStatistics]: Statistics about the extracted procedures, or None if extraction failed.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client or not state.sql_client.engine:
            logger.error("SQL client or engine not initialized")
            raise ValueError("SQL client or engine not initialized")

        prepared_query = prepare_query(
            query=self.fetch_procedure_sql, workflow_args=workflow_args
        )
        statistics = await self.query_executor(
            sql_engine=state.sql_client.engine,
            sql_query=prepared_query,
            workflow_args=workflow_args,
            output_suffix="raw/extras-procedure",
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
            transformed_output (JsonOutput): Output handler for transformed data.
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

        raw_input = ParquetInput(
            path=os.path.join(output_path, "raw"),
            input_prefix=output_prefix,
            file_names=workflow_args.get("file_names"),
            chunk_size=None,
        )
        raw_input = raw_input.get_batched_daft_dataframe()
        transformed_output = JsonOutput(
            output_prefix=output_prefix,
            output_path=output_path,
            output_suffix="transformed",
            typename=typename,
            chunk_start=workflow_args.get("chunk_start"),
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
                await transformed_output.write_daft_dataframe(transform_metadata)
        return await transformed_output.get_statistics()
