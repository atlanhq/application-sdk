from typing import Any, Dict, Optional, Type

import pandas as pd
from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.utils import auto_heartbeater, get_workflow_id
from application_sdk.clients.sql import SQLClient
from application_sdk.common.constants import ApplicationConstants
from application_sdk.common.utils import prepare_query
from application_sdk.decorators import activity_pd
from application_sdk.handlers.sql import SQLHandler
from application_sdk.inputs.json import JsonInput
from application_sdk.inputs.statestore import StateStore
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

    model_config = {"arbitrary_types_allowed": True}

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
    """

    _state: Dict[str, SQLMetadataExtractionActivitiesState] = {}

    fetch_database_sql = None
    fetch_schema_sql = None
    fetch_table_sql = None
    fetch_column_sql = None

    sql_client_class: Type[SQLClient] = SQLClient
    handler_class: Type[SQLHandler] = SQLHandler
    transformer_class: Type[TransformerInterface] = AtlasTransformer

    def __init__(
        self,
        sql_client_class: Type[SQLClient] = SQLClient,
        handler_class: Type[SQLHandler] = SQLHandler,
        transformer_class: Type[TransformerInterface] = AtlasTransformer,
    ):
        self.sql_client_class = sql_client_class
        self.handler_class = handler_class
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
            credentials = StateStore.extract_credentials(
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
        workflow_id = get_workflow_id()
        if workflow_id in self._state:
            await self._state[workflow_id].sql_client.close()

        await super()._clean_state()

    async def _transform_batch(
        self,
        results: pd.DataFrame,
        typename: str,
        workflow_id: str,
        workflow_run_id: str,
        workflow_args: Dict[str, Any],
    ) -> None:
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
    @activity_pd(
        batch_input=lambda self,
        workflow_args,
        state,
        **kwargs: self.sql_client_class.sql_input(
            engine=state.sql_client.engine,
            query=prepare_query(self.fetch_database_sql, workflow_args),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/database",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_databases(
        self, batch_input: pd.DataFrame, raw_output: JsonOutput, **kwargs
    ):
        """Fetch and process databases from the database.

        Args:
            batch_input: DataFrame containing the raw database data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        await raw_output.write_df(batch_input)
        return {
            "chunk_count": raw_output.chunk_count,
            "typename": "database",
            "total_record_count": raw_output.total_record_count,
        }

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self,
        workflow_args,
        state,
        **kwargs: self.sql_client_class.sql_input(
            engine=state.sql_client.engine,
            query=prepare_query(self.fetch_schema_sql, workflow_args),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/schema",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_schemas(
        self, batch_input: pd.DataFrame, raw_output: JsonOutput, **kwargs
    ):
        """Fetch and process schemas.

        Args:
            batch_input: DataFrame containing the raw schema data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        await raw_output.write_df(batch_input)
        return {
            "chunk_count": raw_output.chunk_count,
            "typename": "schema",
            "total_record_count": raw_output.total_record_count,
        }

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self,
        workflow_args,
        state,
        **kwargs: self.sql_client_class.sql_input(
            engine=state.sql_client.engine,
            query=prepare_query(self.fetch_table_sql, workflow_args),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/table",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_tables(
        self, batch_input: pd.DataFrame, raw_output: JsonOutput, **kwargs
    ):
        """Fetch and process tables.

        Args:
            batch_input: DataFrame containing the raw table data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        await raw_output.write_df(batch_input)
        return {
            "chunk_count": raw_output.chunk_count,
            "typename": "table",
            "total_record_count": raw_output.total_record_count,
        }

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self,
        workflow_args,
        state,
        **kwargs: self.sql_client_class.sql_input(
            engine=state.sql_client.engine,
            query=prepare_query(self.fetch_column_sql, workflow_args),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/column",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_columns(
        self, batch_input: pd.DataFrame, raw_output: JsonOutput, **kwargs
    ):
        """Fetch and process columns.

        Args:
            batch_input: DataFrame containing the raw column data.
            raw_output: JsonOutput instance for writing raw data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict containing chunk count, typename, and total record count.
        """
        await raw_output.write_df(batch_input)
        return {
            "chunk_count": raw_output.chunk_count,
            "typename": "column",
            "total_record_count": raw_output.total_record_count,
        }

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        metadata_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/transformed/{workflow_args['typename']}",
            upload_file_prefix=workflow_args["output_prefix"],
            chunk_count=workflow_args["chunk_count"],
            total_record_count=workflow_args["record_count"],
        )
    )
    async def write_type_metadata(self, metadata_output, batch_input=None, **kwargs):
        """Write transformed metadata to output.

        Args:
            metadata_output: JsonOutput instance for writing metadata.
            batch_input: Optional DataFrame containing input data.
            **kwargs: Additional keyword arguments.
        """
        await metadata_output.write_metadata()

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        metadata_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/{workflow_args['typename']}",
            upload_file_prefix=workflow_args["output_prefix"],
            chunk_count=workflow_args["chunk_count"],
            total_record_count=workflow_args["record_count"],
        )
    )
    async def write_raw_type_metadata(
        self,
        metadata_output: JsonOutput,
        batch_input: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        """Writes raw type metadata to output.

        Args:
            metadata_output (JsonOutput): Output handler for metadata.
            batch_input (Optional[Any], optional): Input data. Defaults to None.
            **kwargs: Additional keyword arguments.
        """
        await metadata_output.write_metadata()

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self, workflow_args, **kwargs: JsonInput(
            path=f"{workflow_args['output_path']}/raw/",
            file_suffixes=workflow_args["batch"],
        ),
        transformed_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/transformed/{workflow_args['typename']}",
            upload_file_prefix=workflow_args["output_prefix"],
            chunk_start=workflow_args["chunk_start"],
        ),
    )
    async def transform_data(
        self, batch_input: Any, transformed_output: JsonOutput, **kwargs: Any
    ):
        """Transforms raw data into the required format.

        Args:
            batch_input (Any): Input data to transform.
            transformed_output (JsonOutput): Output handler for transformed data.
            **kwargs: Additional keyword arguments.

        Returns:
            Dict[str, Any]: A dictionary containing:
                - total_record_count: Total number of records processed
                - chunk_count: Number of chunks processed
        """
        typename = kwargs.get("typename")
        workflow_id = kwargs.get("workflow_id")
        workflow_run_id = kwargs.get("workflow_run_id")
        transformed_chunk = await self._transform_batch(
            batch_input,
            typename,
            workflow_id,
            workflow_run_id,
            kwargs,
        )
        await transformed_output.write_df(transformed_chunk)
        return {
            "total_record_count": transformed_output.total_record_count,
            "chunk_count": transformed_output.chunk_count,
        }
