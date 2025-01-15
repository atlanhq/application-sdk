from typing import Any, Dict, Optional, Type

import pandas as pd
from temporalio import activity

from application_sdk import activity_pd
from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.utils import get_workflow_id
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.outputs.json import JsonOutput
from application_sdk.workflows.sql.utils import prepare_query
from application_sdk.workflows.transformers.atlas import AtlasTransformer
from application_sdk.workflows.utils.activity import auto_heartbeater


# ------------------------------ TEMPORARY ------------------------------
class SQLClient:
    def set_credentials(self, credentials: Dict[str, Any]):
        pass

    async def load(self):
        pass

    async def close(self):
        pass


class SQLHandler:
    def set_sql_client(self, sql_client: SQLClient):
        pass


class SQLAuthHandler:
    def set_sql_client(self, sql_client: SQLClient):
        pass


class FetchMetadataHandler:
    def set_sql_client(self, sql_client: SQLClient):
        pass


# ------------------------------ TEMPORARY ------------------------------


class SQLExtractionActivities(ActivitiesInterface):
    state: Dict[str, Any] = {}

    sql_client_class: Type[SQLClient] = SQLClient
    handler_class: Type[SQLHandler] = SQLHandler

    def __init__(
        self,
        sql_client_class: Type[SQLClient] = SQLClient,
        handler_class: Type[SQLHandler] = SQLHandler,
    ):
        self.sql_client_class = sql_client_class
        self.handler_class = handler_class

        super().__init__()

    # State methods
    async def _get_state(self, workflow_args: Dict[str, Any]):
        return await super()._get_state(workflow_args)

    async def _set_state(self, workflow_args: Dict[str, Any]):
        await super()._set_state(workflow_args)

        sql_client = self.sql_client_class()
        await sql_client.load()
        sql_client.set_credentials(workflow_args["credentials"])

        handler = self.handler_class()
        handler.set_sql_client(sql_client)

        self.state[get_workflow_id()] = {
            # Client
            "sql_client": sql_client,
            # Handlers
            "handler": handler,
            # Transformer
            "transformer": AtlasTransformer(
                connector_name=workflow_args["application_name"],
                connector_type="sql",
                tenant_id=workflow_args["tenant_id"],
            ),
        }

    async def _clean_state(self):
        await self.state["sql_client"].close()

        await super()._clean_state()

    async def _transform_batch(
        self,
        results: pd.DataFrame,
        typename: str,
        workflow_id: str,
        workflow_run_id: str,
        workflow_args: Dict[str, Any],
    ) -> None:
        """
        Process a batch of results.

        :param results: The batch of results.
        :param typename: The type of data to fetch.
        :param writer: The writer to use.
        :raises Exception: If the results cannot be processed.
        """
        state = await self._get_state(workflow_args)

        transformed_metadata_list = []
        # Replace NaN with None to avoid issues with JSON serialization
        results = results.replace({float("nan"): None})

        for row in results.to_dict(orient="records"):
            try:
                if not state["transformer"]:
                    raise ValueError("Transformer is not set")

                transformed_metadata: Optional[Dict[str, Any]] = state[
                    "transformer"
                ].transform_metadata(
                    typename,
                    row,
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run_id,
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
    async def preflight_check(self, workflow_args: Dict[str, Any]):
        state = await self._get_state(workflow_args)
        return True

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self, workflow_args: SQLQueryInput(
            engine=self._get_state(workflow_args)["sql_client"].engine,
            query=prepare_query(
                query=self.fetch_database_sql, workflow_args=workflow_args
            ),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/database",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_databases(
        self, batch_input: pd.DataFrame, raw_output: JsonOutput, **kwargs
    ):
        """
        Fetch and process databases from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched databases.
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
        batch_input=lambda self, workflow_args: self._get_state(workflow_args)[
            "sql_client"
        ].sql_input(
            engine=self._get_state(workflow_args)["sql_client"].engine,
            query=SQLWorkflow.prepare_query(
                query=self.fetch_schema_sql, workflow_args=workflow_args
            ),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/schema",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_schemas(
        self, batch_input: pd.DataFrame, raw_output: JsonOutput, **kwargs
    ):
        """
        Fetch and process schemas from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched schemas.
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
        batch_input=lambda self, workflow_args: self._get_state(workflow_args)[
            "sql_client"
        ].sql_input(
            self._get_state(workflow_args)["sql_client"].engine,
            query=prepare_query(
                query=self.fetch_table_sql, workflow_args=workflow_args
            ),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/table",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_tables(
        self, batch_input: pd.DataFrame, raw_output: JsonOutput, **kwargs
    ):
        """
        Fetch and process tables from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched tables.
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
        batch_input=lambda self, workflow_args: self._get_state(workflow_args)[
            "sql_client"
        ].sql_input(
            self._get_state(workflow_args)["sql_client"].engine,
            query=prepare_query(
                query=self.fetch_column_sql, workflow_args=workflow_args
            ),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/column",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_columns(
        self, batch_input: pd.DataFrame, raw_output: JsonOutput, **kwargs
    ):
        """
        Fetch and process columns from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched columns.
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
        self, metadata_output, batch_input=None, **kwargs
    ):
        await metadata_output.write_metadata()

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self, workflow_args: JsonInput(
            path=f"{workflow_args['output_path']}/raw/",
            file_suffixes=workflow_args["batch"],
        ),
        transformed_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/transformed/{workflow_args['typename']}",
            upload_file_prefix=workflow_args["output_prefix"],
            chunk_start=workflow_args["chunk_start"],
        ),
    )
    async def transform_data(self, batch_input, transformed_output, **kwargs):
        typename = kwargs.get("typename")
        workflow_id = kwargs.get("workflow_id")
        workflow_run_id = kwargs.get("workflow_run_id")
        transformed_chunk = await self._transform_batch(
            batch_input, typename, workflow_id, workflow_run_id
        )
        await transformed_output.write_df(transformed_chunk)
        return {
            "total_record_count": transformed_output.total_record_count,
            "chunk_count": transformed_output.chunk_count,
        }
