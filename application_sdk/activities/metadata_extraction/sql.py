from typing import Any, Dict, Generator, Optional, Type

import pandas as pd
from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.utils import auto_heartbeater, get_workflow_id
from application_sdk.clients.sql import SQLClient
from application_sdk.common.constants import ApplicationConstants
from application_sdk.decorators import transform
from application_sdk.handlers.sql import SQLHandler
from application_sdk.inputs.json import JsonInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.outputs.json import JsonOutput
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.atlas import AtlasTransformer


class SQLMetadataExtractionActivitiesState(ActivitiesState):
    """State class for SQL metadata extraction activities.

    This class holds the state required for SQL metadata extraction activities,
    including the SQL client, handler, and transformer instances.

    Attributes:
        sql_client (SQLClient): Client for SQL database operations.
        handler (SQLHandler): Handler for SQL-specific operations.
        transformer (TransformerInterface): Transformer for metadata conversion.
    """

    sql_client: Optional[SQLClient] = None
    handler: Optional[SQLHandler] = None
    transformer: Optional[TransformerInterface] = None


class SQLMetadataExtractionActivities(ActivitiesInterface):
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

    _state: Dict[str, SQLMetadataExtractionActivitiesState] = {}

    fetch_database_sql = None
    fetch_schema_sql = None
    fetch_table_sql = None
    fetch_column_sql = None

    tables_extraction_temp_table_regex_sql = ""
    column_extraction_temp_table_regex_sql = ""

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

        if "credential_guid" in workflow_args:
            credentials = StateStoreInput.extract_credentials(
                workflow_args["credential_guid"]
            )
            await sql_client.load(credentials)

        handler = self.handler_class(sql_client)

        self._state[workflow_id].sql_client = sql_client
        self._state[workflow_id].handler = handler
        self._state[workflow_id].transformer = self.transformer_class(
            connector_name=ApplicationConstants.APPLICATION_NAME.value,
            connector_type="sql",
            tenant_id=ApplicationConstants.TENANT_ID.value,
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

    async def _transform_batch(
        self,
        results,
        typename: str,
        workflow_id: str,
        workflow_run_id: str,
        workflow_args: Dict[str, Any],
    ):
        """Transform a batch of results into metadata.

        Args:
            results: The DataFrame containing the batch of results to transform.
            typename: The type of data being transformed (e.g., 'database', 'schema', 'table').
            workflow_id: The ID of the current workflow.
            workflow_run_id: The run ID of the current workflow.
            workflow_args: Dictionary containing workflow arguments and configuration.

        Returns:
            None

        Raises:
            ValueError: If the transformer is not properly set.
        """

        state: SQLMetadataExtractionActivitiesState = await self._get_state(
            workflow_args
        )

        connection_name = workflow_args.get("connection", {}).get(
            "connection_name", None
        )
        connection_qualified_name = workflow_args.get("connection", {}).get(
            "connection_qualified_name", None
        )

        transformed_metadata_list = []
        # Replace NaN with None to avoid issues with JSON serialization
        results = results.replace({float("nan"): None})

        for row in results.to_dict(orient="records"):
            try:
                if not state.transformer:
                    raise ValueError("Transformer is not set")

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
                if transformed_metadata is not None:
                    transformed_metadata_list.append(transformed_metadata)
                else:
                    activity.logger.warning(f"Skipped invalid {typename} data: {row}")
            except Exception as row_error:
                activity.logger.error(
                    f"Error processing row for {typename}: {row_error}"
                )
        return pd.DataFrame(transformed_metadata_list)

    @activity.defn
    @auto_heartbeater
    @transform(
        batch_input=SQLQueryInput(query="fetch_database_sql"),
        raw_output=JsonOutput(output_suffix="/raw/database"),
    )
    async def fetch_databases(
        self,
        batch_input: Generator[pd.DataFrame, None, None],
        raw_output: JsonOutput,
        **kwargs: Dict[str, Any],
    ):
        """Fetch databases from the source database.

        Args:
            batch_input: DataFrame containing the raw database data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        await raw_output.write_batched_dataframe(batch_input)
        return raw_output.get_metadata(typename="database")

    @activity.defn
    @auto_heartbeater
    @transform(
        batch_input=SQLQueryInput(query="fetch_schema_sql"),
        raw_output=JsonOutput(output_suffix="/raw/schema"),
    )
    async def fetch_schemas(
        self,
        batch_input: Generator[pd.DataFrame, None, None],
        raw_output: JsonOutput,
        **kwargs: Dict[str, Any],
    ):
        """Fetch schemas from the source database.

        Args:
            batch_input: DataFrame containing the raw schema data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        await raw_output.write_batched_dataframe(batch_input)
        return raw_output.get_metadata(typename="schema")

    @activity.defn
    @auto_heartbeater
    @transform(
        batch_input=SQLQueryInput(
            query="fetch_table_sql",
            temp_table_sql_query="tables_extraction_temp_table_regex_sql",
        ),
        raw_output=JsonOutput(output_suffix="/raw/table"),
    )
    async def fetch_tables(
        self,
        batch_input: Generator[pd.DataFrame, None, None],
        raw_output: JsonOutput,
        **kwargs: Dict[str, Any],
    ):
        """Fetch tables from the source database.

        Args:
            batch_input: DataFrame containing the raw table data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        await raw_output.write_batched_dataframe(batch_input)
        return raw_output.get_metadata(typename="table")

    @activity.defn
    @auto_heartbeater
    @transform(
        batch_input=SQLQueryInput(
            query="fetch_column_sql",
            temp_table_sql_query="column_extraction_temp_table_regex_sql",
        ),
        raw_output=JsonOutput(output_suffix="/raw/column"),
    )
    async def fetch_columns(
        self,
        batch_input: Generator[pd.DataFrame, None, None],
        raw_output: JsonOutput,
        **kwargs: Dict[str, Any],
    ):
        """Fetch columns from the source database.

        Args:
            batch_input: DataFrame containing the raw column data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        await raw_output.write_batched_dataframe(batch_input)
        return raw_output.get_metadata(typename="column")

    @activity.defn
    @auto_heartbeater
    @transform(metadata_output=JsonOutput(output_suffix="/transformed"))
    async def write_type_metadata(
        self, metadata_output: JsonOutput, **kwargs: Dict[str, Any]
    ):
        """Write transformed metadata to output.

        Args:
            metadata_output: JsonOutput instance for writing metadata.
            batch_input: Optional DataFrame containing input data.
            **kwargs: Additional keyword arguments.
        """
        await metadata_output.write_metadata()

    @activity.defn
    @auto_heartbeater
    @transform(metadata_output=JsonOutput(output_suffix="/raw"))
    async def write_raw_type_metadata(
        self, metadata_output: JsonOutput, **kwargs: Dict[str, Any]
    ):
        """Write raw metadata to the specified output destination.

        This activity writes the metadata of raw entities to the configured output path.
        It is typically used to store raw data fetched from the source database before
        any transformations are applied.

        Args:
            metadata_output (JsonOutput): Output handler for metadata.
            batch_input (Optional[Any], optional): Input data. Defaults to None.
            **kwargs: Additional keyword arguments.
        """
        await metadata_output.write_metadata()

    @activity.defn
    @auto_heartbeater
    @transform(
        raw_input=JsonInput(path="/raw/"),
        transformed_output=JsonOutput(output_suffix="/transformed/"),
    )
    async def transform_data(
        self,
        raw_input: Generator[pd.DataFrame, None, None],
        transformed_output: JsonOutput,
        **kwargs: Dict[str, Any],
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
        for input in raw_input:
            transformed_chunk = await self._transform_batch(
                input,
                kwargs.get("typename"),
                kwargs.get("workflow_id"),
                kwargs.get("workflow_run_id"),
                kwargs,
            )
            await transformed_output.write_dataframe(transformed_chunk)
        return transformed_output.get_metadata()
