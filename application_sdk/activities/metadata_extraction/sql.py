import os
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
    overload,
)

from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import (
    auto_heartbeater,
    get_object_store_prefix,
    get_workflow_id,
)
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.common.dataframe_utils import is_empty_dataframe
from application_sdk.common.error_codes import ActivityError
from application_sdk.common.utils import (
    get_database_names,
    parse_credentials_extra,
    prepare_query,
    read_sql_files,
)
from application_sdk.constants import APP_TENANT_ID, APPLICATION_NAME, SQL_QUERIES_PATH
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.inputs.parquet import ParquetInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.json import JsonOutput
from application_sdk.outputs.parquet import ParquetOutput
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
        """
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = BaseSQLMetadataExtractionActivitiesState()

        await super()._set_state(workflow_args)

        sql_client = self.sql_client_class()

        # Load credentials BEFORE creating handler to avoid race condition
        if "credential_guid" in workflow_args:
            credentials = await SecretStore.get_credentials(
                workflow_args["credential_guid"]
            )
            await sql_client.load(credentials)

        # Assign sql_client and handler to state AFTER credentials are loaded
        self._state[workflow_id].sql_client = sql_client
        handler = self.handler_class(sql_client)
        self._state[workflow_id].handler = handler

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
        sql_engine: Any,
        sql_query: Optional[str],
        workflow_args: Dict[str, Any],
        output_suffix: str,
        typename: str,
        write_to_file: bool = True,
        concatenate: bool = False,
        return_dataframe: bool = False,
        sql_client: Optional[BaseSQLClient] = None,
    ) -> Optional[ActivityStatistics]: ...

    @overload
    async def query_executor(
        self,
        sql_engine: Any,
        sql_query: Optional[str],
        workflow_args: Dict[str, Any],
        output_suffix: str,
        typename: str,
        write_to_file: bool = True,
        concatenate: bool = False,
        return_dataframe: bool = True,
        sql_client: Optional[BaseSQLClient] = None,
    ) -> Optional[Union[ActivityStatistics, "pd.DataFrame"]]: ...

    async def query_executor(
        self,
        sql_engine: Any,
        sql_query: Optional[str],
        workflow_args: Dict[str, Any],
        output_suffix: str,
        typename: str,
        write_to_file: bool = True,
        concatenate: bool = False,
        return_dataframe: bool = False,
        sql_client: Optional[BaseSQLClient] = None,
    ) -> Optional[Union[ActivityStatistics, "pd.DataFrame"]]:
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
            Optional[Union[ActivityStatistics, pd.DataFrame]]: Statistics about the generated Parquet file,
            or a DataFrame if return_dataframe=True, or None if the query is empty or execution fails.

        Raises:
            ValueError: If `sql_engine` is not provided.
        """
        # Common pre-checks and setup shared by both multidb and single-db paths
        if not sql_query:
            logger.warning("Query is empty, skipping execution.")
            return None

        if not sql_engine:
            logger.error("SQL engine is not set.")
            raise ValueError("SQL engine must be provided.")

        # Setup parquet output using helper method
        parquet_output = self._setup_parquet_output(
            workflow_args, output_suffix, write_to_file
        )

        # If multidb mode is enabled, run per-database flow
        if getattr(self, "multidb", False):
            return await self._execute_multidb_flow(
                sql_client,
                sql_query,
                workflow_args,
                output_suffix,
                typename,
                write_to_file,
                concatenate,
                return_dataframe,
                parquet_output,
            )

        # Single-db execution path
        # Prepare query for single-db execution
        prepared_query = self._prepare_database_query(
            sql_query, None, workflow_args, typename
        )

        # Execute using helper method
        success, _ = await self._execute_single_db(
            sql_engine, prepared_query, parquet_output, write_to_file
        )

        if not success:
            logger.error("Failed to execute single-db query")
            return None

        if parquet_output:
            logger.info(
                f"Successfully wrote query results to {parquet_output.get_full_path()}"
            )
            return await parquet_output.get_statistics(typename=typename)

        logger.warning("No parquet output configured for single-db execution")
        return None

    def _setup_parquet_output(
        self,
        workflow_args: Dict[str, Any],
        output_suffix: str,
        write_to_file: bool,
    ) -> Optional[ParquetOutput]:
        if not write_to_file:
            return None
        output_prefix = workflow_args.get("output_prefix")
        output_path = workflow_args.get("output_path")
        if not output_prefix or not output_path:
            logger.error("Output prefix or path not provided in workflow_args.")
            raise ValueError(
                "Output prefix and path must be specified in workflow_args."
            )
        return ParquetOutput(
            output_path=output_path,
            output_suffix=output_suffix,
            use_consolidation=True,
        )

    def _get_temp_table_regex_sql(self, typename: str) -> str:
        """Get the appropriate temp table regex SQL based on typename."""
        if typename == "column":
            return self.extract_temp_table_regex_column_sql or ""
        elif typename == "table":
            return self.extract_temp_table_regex_table_sql or ""
        else:
            return ""

    def _prepare_database_query(
        self,
        sql_query: str,
        database_name: Optional[str],
        workflow_args: Dict[str, Any],
        typename: str,
        use_posix_regex: bool = False,
    ) -> Optional[str]:
        """Prepare query for database execution with proper substitutions."""
        # Replace database name placeholder if provided
        fetch_sql = sql_query
        if database_name:
            fetch_sql = fetch_sql.replace("{database_name}", database_name)

        # Get temp table regex SQL
        temp_table_regex_sql = self._get_temp_table_regex_sql(typename)

        # Prepare the query
        prepared_query = prepare_query(
            query=fetch_sql,
            workflow_args=workflow_args,
            temp_table_regex_sql=temp_table_regex_sql,
            use_posix_regex=use_posix_regex,
        )

        if prepared_query is None:
            db_context = f" for database {database_name}" if database_name else ""
            raise ValueError(f"Failed to prepare query{db_context}")

        return prepared_query

    async def _setup_database_connection(
        self,
        sql_client: BaseSQLClient,
        database_name: str,
    ) -> None:
        """Setup connection for a specific database."""
        extra = parse_credentials_extra(sql_client.credentials)
        extra["database"] = database_name
        sql_client.credentials["extra"] = extra
        await sql_client.load(sql_client.credentials)

    # NOTE: Consolidated: per-database processing is now inlined in the multi-DB loop

    async def _finalize_multidb_results(
        self,
        write_to_file: bool,
        concatenate: bool,
        return_dataframe: bool,
        parquet_output: Optional[ParquetOutput],
        dataframe_list: List[
            Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]
        ],
        workflow_args: Dict[str, Any],
        output_suffix: str,
        typename: str,
    ) -> Optional[Union[ActivityStatistics, "pd.DataFrame"]]:
        """Finalize results for multi-database execution."""
        if write_to_file and parquet_output:
            return await parquet_output.get_statistics(typename=typename)

        if not write_to_file and concatenate:
            try:
                import pandas as pd  # type: ignore

                valid_dataframes: List[pd.DataFrame] = []
                for df_generator in dataframe_list:
                    if df_generator is None:
                        continue
                    for dataframe in df_generator:  # type: ignore[assignment]
                        if dataframe is None:
                            continue
                        if hasattr(dataframe, "empty") and getattr(dataframe, "empty"):
                            continue
                        valid_dataframes.append(dataframe)

                if not valid_dataframes:
                    logger.warning(
                        "No valid dataframes collected across databases for concatenation"
                    )
                    return None

                concatenated = pd.concat(valid_dataframes, ignore_index=True)

                if return_dataframe:
                    return concatenated  # type: ignore[return-value]

                # Create new parquet output for concatenated data
                concatenated_parquet_output = self._setup_parquet_output(
                    workflow_args, output_suffix, True
                )
                if concatenated_parquet_output:
                    await concatenated_parquet_output.write_dataframe(concatenated)  # type: ignore[arg-type]
                    return await concatenated_parquet_output.get_statistics(
                        typename=typename
                    )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"Error concatenating multi-DB dataframes: {str(e)}",
                    exc_info=True,
                )
                raise

        logger.warning(
            "multidb execution returned no output to write (write_to_file=False, concatenate=False)"
        )
        return None

    async def _execute_multidb_flow(
        self,
        sql_client: Optional[BaseSQLClient],
        sql_query: str,
        workflow_args: Dict[str, Any],
        output_suffix: str,
        typename: str,
        write_to_file: bool,
        concatenate: bool,
        return_dataframe: bool,
        parquet_output: Optional[ParquetOutput],
    ) -> Optional[Union[ActivityStatistics, "pd.DataFrame"]]:
        """Execute multi-database flow with proper error handling and result finalization."""
        # Get effective SQL client
        effective_sql_client = sql_client
        if effective_sql_client is None:
            state = cast(
                BaseSQLMetadataExtractionActivitiesState,
                await self._get_state(workflow_args),
            )
            effective_sql_client = state.sql_client

        if not effective_sql_client:
            logger.error("SQL client not initialized for multidb execution")
            raise ValueError("SQL client not initialized")

        # Resolve databases to iterate
        database_names = await get_database_names(
            effective_sql_client, workflow_args, self.fetch_database_sql
        )
        if not database_names:
            logger.warning("No databases found to process")
            return None

        # Validate client
        if not effective_sql_client.engine:
            logger.error("SQL client engine not initialized")
            raise ValueError("SQL client engine not initialized")

        successful_databases: List[str] = []
        dataframe_list: List[
            Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]
        ] = []

        # Iterate databases and execute (consolidated single-db processing)
        for database_name in database_names or []:
            try:
                # Setup connection for this database
                await self._setup_database_connection(
                    effective_sql_client, database_name
                )

                # Prepare query for this database
                prepared_query = self._prepare_database_query(
                    sql_query,
                    database_name,
                    workflow_args,
                    typename,
                    use_posix_regex=True,
                )

                # Execute using helper method
                success, batched_iterator = await self._execute_single_db(
                    effective_sql_client.engine,
                    prepared_query,
                    parquet_output,
                    write_to_file,
                )

                if success:
                    logger.info(f"Successfully processed database: {database_name}")

            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"Failed to process database '{database_name}': {str(e)}. Failing the workflow.",
                    exc_info=True,
                )
                raise

            if success:
                successful_databases.append(database_name)
                if not write_to_file and batched_iterator:
                    dataframe_list.append(batched_iterator)

        # Log results
        logger.info(
            f"Successfully processed {len(successful_databases)} databases: {successful_databases}"
        )

        # Finalize results
        return await self._finalize_multidb_results(
            write_to_file,
            concatenate,
            return_dataframe,
            parquet_output,
            dataframe_list,
            workflow_args,
            output_suffix,
            typename,
        )

    async def _execute_single_db(
        self,
        sql_engine: Any,
        prepared_query: Optional[str],
        parquet_output: Optional[ParquetOutput],
        write_to_file: bool,
    ) -> Tuple[
        bool, Optional[Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]]
    ]:  # type: ignore
        if not prepared_query:
            logger.error("Prepared query is None, cannot execute")
            return False, None

        try:
            sql_input = SQLQueryInput(engine=sql_engine, query=prepared_query)
            batched_iterator = await sql_input.get_batched_dataframe()

            if write_to_file and parquet_output:
                await parquet_output.write_batched_dataframe(batched_iterator)  # type: ignore
                return True, None

            return True, batched_iterator
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
            file_names=workflow_args.get("file_names"),
        )
        raw_input = raw_input.get_batched_daft_dataframe()

        transformed_output = JsonOutput(
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
