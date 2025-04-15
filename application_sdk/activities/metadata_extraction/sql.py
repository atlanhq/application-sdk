import os
from typing import Any, Dict, Iterator, Optional, Type, Tuple

import daft
from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.utils import auto_heartbeater, get_workflow_id
from application_sdk.clients.sql import SQLClient
from application_sdk.common.constants import ApplicationConstants
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.utils import prepare_query
from application_sdk.handlers.sql import SQLHandler
from application_sdk.inputs.parquet import ParquetInput
from application_sdk.inputs.secretstore import SecretStoreInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.outputs.json import JsonOutput
from application_sdk.outputs.parquet import ParquetOutput
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.atlas import AtlasTransformer

activity.logger = get_logger(__name__)


class SQLMetadataExtractionActivitiesState(ActivitiesState[SQLHandler]):
    """State class for SQL metadata extraction activities.

    This class holds the state required for SQL metadata extraction activities,
    including the SQL client, handler, and transformer instances.

    Attributes:
        sql_client (SQLClient): Client for SQL database operations.
        handler (SQLHandler): Handler for SQL-specific operations.
        transformer (TransformerInterface): Transformer for metadata conversion.
    """

    sql_client: Optional[SQLClient] = None
    transformer: Optional[TransformerInterface] = None


class SQLMetadataExtractionActivities(ActivitiesInterface[SQLHandler]):
    """Activities for extracting metadata from SQL databases.

    This class provides activities for extracting metadata from SQL databases,
    including databases, schemas, tables, and columns. It supports customization
    of the SQL client, handler, and transformer classes.

    Attributes:
        fetch_database_sql (Optional[str]): SQL query for fetching databases.
        fetch_schema_sql (Optional[str]): SQL query for fetching schemas.
        fetch_table_sql (Optional[str]): SQL query for fetching tables.
        fetch_column_sql (Optional[str]): SQL query for fetching columns.
        sql_client_class (Type[SQLClient]): Class for SQL client operations.
        handler_class (Type[SQLHandler]): Class for SQL handling operations.
        transformer_class (Type[TransformerInterface]): Class for metadata transformation.
        tables_extraction_temp_table_regex_sql (str): SQL snippet for excluding temporary tables during tables extraction.
            Defaults to an empty string.
        column_extraction_temp_table_regex_sql (str): SQL snippet for excluding temporary tables during column extraction.
            Defaults to an empty string.
    """

    fetch_database_sql: Optional[str] = None
    fetch_schema_sql: Optional[str] = None
    fetch_table_sql: Optional[str] = None
    fetch_column_sql: Optional[str] = None

    tables_extraction_temp_table_regex_sql: str = ""
    column_extraction_temp_table_regex_sql: str = ""

    sql_client_class: Type[SQLClient] = SQLClient
    handler_class: Type[SQLHandler] = SQLHandler
    transformer_class: Type[TransformerInterface] = AtlasTransformer

    def __init__(
        self,
        sql_client_class: Optional[Type[SQLClient]] = None,
        handler_class: Optional[Type[SQLHandler]] = None,
        transformer_class: Optional[Type[TransformerInterface]] = None,
    ):
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
            SQLMetadataExtractionActivitiesState: The current state.
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
            self._state[workflow_id] = SQLMetadataExtractionActivitiesState()

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
            "connector_name": ApplicationConstants.APPLICATION_NAME.value,
            "connector_type": "sql",
            "tenant_id": ApplicationConstants.TENANT_ID.value,
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
            if workflow_id in self._state:
                await self._state[workflow_id].sql_client.close()
        except Exception as e:
            activity.logger.warning("Failed to close SQL client", exc_info=e)

        await super()._clean_state()

    def _process_rows(
        self,
        results: "daft.DataFrame",
        typename: str,
        workflow_id: str,
        workflow_run_id: str,
        state: SQLMetadataExtractionActivitiesState,
        connection_name: Optional[str],
        connection_qualified_name: Optional[str],
    ) -> Iterator[Dict[str, Any]]:
        """Process DataFrame rows and transform them into metadata.

        Args:
            results: DataFrame containing the rows to process
            typename: Type of data being transformed
            workflow_id: Current workflow ID
            workflow_run_id: Current workflow run ID
            state: Current activity state
            connection_name: Name of the connection
            connection_qualified_name: Qualified name of the connection

        Returns:
            list: List of transformed metadata dictionaries
        """
        if not state.transformer:
            raise ValueError("Transformer is not set")
        for row in results.iter_rows():
            try:
                transformed_metadata: Optional[Dict[str, Any]] = (
                    state.transformer.transform_metadata(
                        typename,
                        row,
                        workflow_id=workflow_id,
                        workflow_run_id=workflow_run_id,
                        connection_name=connection_name,
                        connection_qualified_name=connection_qualified_name,
                    )
                )
                if transformed_metadata:
                    yield transformed_metadata
                else:
                    activity.logger.warning(f"Skipped invalid {typename} data: {row}")
            except Exception as row_error:
                activity.logger.error(
                    f"Error processing row for {typename}: {row_error}"
                )

    def _transform_batch(
        self,
        results: "daft.DataFrame",
        typename: str,
        state: SQLMetadataExtractionActivitiesState,
        workflow_id: str,
        workflow_run_id: str,
        workflow_args: Dict[str, Any],
    ):
        connection_name = workflow_args.get("connection", {}).get(
            "connection_name", None
        )
        connection_qualified_name = workflow_args.get("connection", {}).get(
            "connection_qualified_name", None
        )

        yield from self._process_rows(
            results,
            typename,
            workflow_id,
            workflow_run_id,
            state,
            connection_name,
            connection_qualified_name,
        )

    def _validate_output_args(self, workflow_args: Dict[str, Any]) -> Tuple[str, str]:
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
        if not output_prefix or not output_path:
            activity.logger.warning("No output prefix or path provided")
            raise ValueError("No output prefix or path provided")
        return output_prefix, output_path

    def _validate_query(self, query_template: Optional[str], entity_type: str) -> None:
        """Validates that a query template exists.
        
        Args:
            query_template: SQL query template to validate.
            entity_type: Type of entity (database, schema, table, column).
            
        Raises:
            ValueError: If query_template is None.
        """
        if not query_template:
            activity.logger.warning(f"No {entity_type} query provided")
            raise ValueError(f"No {entity_type} query provided")

    def _validate_prepared_query(self, query: str, entity_type: str) -> None:
        """Validates that a prepared query exists.
        
        Args:
            query: Prepared SQL query to validate.
            entity_type: Type of entity (database, schema, table, column).
            
        Raises:
            ValueError: If query is empty.
        """
        if not query:
            activity.logger.warning(f"No {entity_type} query provided")
            raise ValueError(f"No {entity_type} query provided")

    @activity.defn
    @auto_heartbeater
    async def fetch_databases(self, workflow_args: Dict[str, Any]):
        """Fetch databases from the source database.

        Args:
            batch_input: DataFrame containing the raw database data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        self._validate_query(self.fetch_database_sql, "database")

        output_prefix, output_path = self._validate_output_args(workflow_args)

        state = await self._get_state(workflow_args)

        query = prepare_query(
            query=self.fetch_database_sql, workflow_args=workflow_args
        )

        self._validate_prepared_query(query, "database")

        sql_input = SQLQueryInput(
            engine=state.sql_client.engine,
            query=query,
            chunk_size=None,
        )
        sql_input = await sql_input.get_daft_dataframe()

        raw_output = ParquetOutput(
            output_prefix=output_prefix,
            output_path=output_path,
            output_suffix="raw/database",
        )
        await raw_output.write_daft_dataframe(sql_input)
        return await raw_output.get_statistics(typename="database")

    @activity.defn
    @auto_heartbeater
    async def fetch_schemas(self, workflow_args: Dict[str, Any]):
        """Fetch schemas from the source database.

        Args:
            batch_input: DataFrame containing the raw schema data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        self._validate_query(self.fetch_schema_sql, "schema")

        output_prefix, output_path = self._validate_output_args(workflow_args)

        state = await self._get_state(workflow_args)

        query = prepare_query(query=self.fetch_schema_sql, workflow_args=workflow_args)

        self._validate_prepared_query(query, "schema")

        sql_input = SQLQueryInput(
            engine=state.sql_client.engine,
            query=query,
            chunk_size=None,
        )
        sql_input = await sql_input.get_daft_dataframe()

        raw_output = ParquetOutput(
            output_prefix=output_prefix,
            output_path=output_path,
            output_suffix="raw/schema",
        )
        await raw_output.write_daft_dataframe(sql_input)
        return await raw_output.get_statistics(typename="schema")

    @activity.defn
    @auto_heartbeater
    async def fetch_tables(self, workflow_args: Dict[str, Any]):
        """Fetch tables from the source database.

        Args:
            batch_input: DataFrame containing the raw table data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        self._validate_query(self.fetch_table_sql, "table")

        output_prefix, output_path = self._validate_output_args(workflow_args)

        state = await self._get_state(workflow_args)

        query = prepare_query(query=self.fetch_table_sql, workflow_args=workflow_args)

        self._validate_prepared_query(query, "table")

        sql_input = SQLQueryInput(
            engine=state.sql_client.engine,
            query=query,
            chunk_size=None,
        )
        sql_input = await sql_input.get_daft_dataframe()

        raw_output = ParquetOutput(
            output_prefix=output_prefix,
            output_path=output_path,
            output_suffix="raw/table",
        )
        await raw_output.write_daft_dataframe(sql_input)
        return await raw_output.get_statistics(typename="table")

    @activity.defn
    @auto_heartbeater
    async def fetch_columns(self, workflow_args: Dict[str, Any]):
        """Fetch columns from the source database.

        Args:
            batch_input: DataFrame containing the raw column data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        self._validate_query(self.fetch_column_sql, "column")

        output_prefix, output_path = self._validate_output_args(workflow_args)

        state = await self._get_state(workflow_args)

        query = prepare_query(
            query=self.fetch_column_sql,
            workflow_args=workflow_args,
            temp_table_regex_sql=self.column_extraction_temp_table_regex_sql,
        )

        self._validate_prepared_query(query, "column")

        sql_input = SQLQueryInput(
            engine=state.sql_client.engine,
            query=query,
            chunk_size=None,
        )
        sql_input = await sql_input.get_daft_dataframe()

        raw_output = ParquetOutput(
            output_prefix=output_prefix,
            output_path=output_path,
            output_suffix="raw/column",
        )
        await raw_output.write_daft_dataframe(sql_input)
        return await raw_output.get_statistics(typename="column")

    @activity.defn
    @auto_heartbeater
    async def transform_data(
        self,
        workflow_args: Dict[str, Any],
    ):
        """Transforms raw data into the required format.

        Args:
            raw_input (Any): Input data to transform.
            transformed_output (JsonOutput): Output handler for transformed data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict[str, Any]: A dictionary containing:
                - total_record_count: Total number of records processed
                - chunk_count: Number of chunks processed
        """

        output_prefix, output_path = self._validate_output_args(workflow_args)
        
        typename = workflow_args.get("typename")
        workflow_id = workflow_args.get("workflow_id")
        workflow_run_id = workflow_args.get("workflow_run_id")
        if not typename or not workflow_id or not workflow_run_id:
            activity.logger.warning("Missing required workflow arguments")
            raise ValueError("Missing required workflow arguments")

        state: SQLMetadataExtractionActivitiesState = await self._get_state(
            workflow_args
        )

        raw_input = ParquetInput(
            path=os.path.join(output_path, "raw"),
            input_prefix=output_prefix,
            file_names=workflow_args.get("file_names"),
            chunk_size=None,
        )
        raw_input = await raw_input.get_daft_dataframe()

        transformed_chunk = self._transform_batch(
            raw_input,
            typename,
            state,
            workflow_id,
            workflow_run_id,
            workflow_args,
        )

        transformed_output = JsonOutput(
            output_prefix=output_prefix,
            output_path=output_path,
            output_suffix="transformed",
            typename=typename,
        )
        await transformed_output.write_daft_dataframe(transformed_chunk)
        return await transformed_output.get_statistics()
