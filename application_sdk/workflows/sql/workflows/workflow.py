import asyncio
import logging
from datetime import timedelta
from typing import Any, Callable, Coroutine, Dict, List, Optional

import pandas as pd
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from application_sdk import activity_pd
from application_sdk.clients.sql_resource import SQLResource, SQLResourceConfig
from application_sdk.clients.temporal_resource import TemporalConfig, TemporalResource
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.json import JsonInput
from application_sdk.inputs.statestore import StateStore
from application_sdk.outputs.json import JsonOutput
from application_sdk.workflows.sql.utils import prepare_filters
from application_sdk.workflows.transformers import TransformerInterface
from application_sdk.workflows.utils.activity import auto_heartbeater
from application_sdk.workflows.workflow import WorkflowInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


@workflow.defn
class SQLWorkflow(WorkflowInterface):
    fetch_database_sql = ""
    fetch_schema_sql = ""
    fetch_table_sql = ""
    fetch_column_sql = ""

    sql_resource: SQLResource | None = None
    transformer: TransformerInterface | None = None

    application_name: str = "sql-connector"
    batch_size: int = 100000
    max_transform_concurrency: int = 5

    # Note: the defaults are passed as temporal tries to initialize the workflow with no args
    def __init__(self):
        super().__init__()

    def set_sql_resource(self, sql_resource: SQLResource) -> "SQLWorkflow":
        self.sql_resource = sql_resource
        return self

    def set_transformer(self, transformer: TransformerInterface) -> "SQLWorkflow":
        self.transformer = transformer
        return self

    def set_application_name(self, application_name: str) -> "SQLWorkflow":
        self.application_name = application_name
        return self

    def set_batch_size(self, batch_size: int) -> "SQLWorkflow":
        self.batch_size = batch_size
        return self

    def set_max_transform_concurrency(
        self, max_transform_concurrency: int
    ) -> "SQLWorkflow":
        self.max_transform_concurrency = max_transform_concurrency
        return self

    def set_temporal_resource(
        self, temporal_resource: TemporalResource
    ) -> "SQLWorkflow":
        super().set_temporal_resource(temporal_resource)
        return self

    def get_activities(self) -> List[Callable[..., Any]]:
        return [
            self.set_workflow_activity_context,
            self.fetch_databases,
            self.fetch_schemas,
            self.fetch_tables,
            self.fetch_columns,
            self.transform_data,
            self.write_type_metadata,
            self.write_raw_type_metadata,
        ] + super().get_activities()

    async def start(
        self, workflow_args: Dict[str, Any], workflow_class: Any | None = None
    ) -> Dict[str, Any]:
        """
        Run the workflow.

        :param workflow_args: The workflow arguments.
        :return: The workflow results.
        """
        if self.sql_resource is None:
            raise ValueError("SQL resource is not set")

        workflow_class = workflow_class or self.__class__

        return await super().start(workflow_args, workflow_class)

    async def _transform_batch(
        self,
        results: pd.DataFrame,
        typename: str,
        workflow_id: str,
        workflow_run_id: str,
    ) -> None:
        """
        Process a batch of results.

        :param results: The batch of results.
        :param typename: The type of data to fetch.
        :param writer: The writer to use.
        :raises Exception: If the results cannot be processed.
        """
        if self.transformer is None:
            raise ValueError("Transformer is not set")

        transformed_metadata_list = []
        # Replace NaN with None to avoid issues with JSON serialization
        results = results.replace({float("nan"): None})

        for row in results.to_dict(orient="records"):
            try:
                if not self.transformer:
                    raise ValueError("Transformer is not set")

                transformed_metadata: Optional[Dict[str, Any]] = (
                    self.transformer.transform_metadata(
                        typename,
                        row,
                        workflow_id=workflow_id,
                        workflow_run_id=workflow_run_id,
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

    @staticmethod
    def prepare_query(query: str, workflow_args: Dict[str, Any]) -> str:
        """
        Method to prepare the query with the include and exclude filters.
        Only fetches all metadata when both include and exclude filters are empty.
        """
        try:
            metadata = workflow_args.get("metadata", workflow_args.get("form_data", {}))

            # using "or" instead of default correct defaults are set in case of empty string
            include_filter = metadata.get("include_filter") or "{}"
            exclude_filter = metadata.get("exclude_filter") or "{}"
            temp_table_regex = metadata.get("temp_table_regex") or "^$"

            normalized_include_regex, normalized_exclude_regex = prepare_filters(
                include_filter, exclude_filter
            )

            exclude_empty_tables = workflow_args.get("metadata", {}).get(
                "exclude_empty_tables", False
            )
            exclude_views = workflow_args.get("metadata", {}).get(
                "exclude_views", False
            )

            return query.format(
                normalized_include_regex=normalized_include_regex,
                normalized_exclude_regex=normalized_exclude_regex,
                exclude_table=temp_table_regex,
                exclude_empty_tables=exclude_empty_tables,
                exclude_views=exclude_views,
            )
        except Exception as e:
            logger.error(f"Error preparing query [{query}]:  {e}")
            return None

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            engine=self.sql_resource.engine,
            query=SQLWorkflow.prepare_query(
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
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            engine=self.sql_resource.engine,
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
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            self.sql_resource.engine,
            query=SQLWorkflow.prepare_query(
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
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            self.sql_resource.engine,
            query=SQLWorkflow.prepare_query(
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

    def get_transform_batches(self, chunk_count: int, typename: str):
        # concurrency logic
        concurrency_level = min(
            self.max_transform_concurrency,
            chunk_count,
        )

        batches: List[List[str]] = []
        chunk_start_numbers: List[int] = []
        start = 0
        for i in range(concurrency_level):
            current_batch_start = start
            chunk_start_numbers.append(current_batch_start)
            current_batch_count = int(chunk_count / concurrency_level)
            if i < chunk_count % concurrency_level and chunk_count > concurrency_level:
                current_batch_count += 1

            batches.append(
                [
                    f"{typename}/{i}.json"
                    for i in range(
                        current_batch_start + 1,
                        current_batch_start + current_batch_count + 1,
                    )
                ]
            )
            start += current_batch_count

        return batches, chunk_start_numbers

    async def fetch_and_transform(
        self,
        fetch_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]],
        workflow_args: Dict[str, Any],
        retry_policy: RetryPolicy,
    ) -> None:
        raw_stat = await workflow.execute_activity(
            fetch_fn,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )
        transform_activities: List[Any] = []

        if raw_stat is None or len(raw_stat) == 0:
            # to handle the case where the fetch_fn returns None or []
            return

        chunk_count = max(value.get("chunk_count", 0) for value in raw_stat)
        if chunk_count is None:
            raise ValueError("Invalid chunk_count")

        raw_total_record_count = max(
            value.get("total_record_count", 0) for value in raw_stat
        )
        if raw_total_record_count is None:
            raise ValueError("Invalid raw_total_record_count")

        if chunk_count == 0:
            return

        typename = raw_stat[0].get("typename")
        if typename is None:
            raise ValueError("Invalid typename")

        # Write the raw metadata
        await workflow.execute_activity(
            self.write_raw_type_metadata,
            {
                "record_count": raw_total_record_count,
                "chunk_count": chunk_count,
                "typename": typename,
                **workflow_args,
            },
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        batches, chunk_starts = self.get_transform_batches(chunk_count, typename)

        for i in range(len(batches)):
            transform_activities.append(
                workflow.execute_activity(
                    self.transform_data,
                    {
                        "typename": typename,
                        "batch": batches[i],
                        "chunk_start": chunk_starts[i],
                        **workflow_args,
                    },
                    retry_policy=retry_policy,
                    start_to_close_timeout=timedelta(seconds=1000),
                )
            )

        record_counts = await asyncio.gather(*transform_activities)

        # Calculate the parameters necessary for writing metadata
        total_record_count = sum(
            max(
                record_output.get("total_record_count", 0)
                for record_output in record_count
            )
            for record_count in record_counts
        )
        chunk_count = sum(
            max(record_output.get("chunk_count", 0) for record_output in record_count)
            for record_count in record_counts
        )

        # Write the transformed metadata
        await workflow.execute_activity(
            self.write_type_metadata,
            {
                "record_count": total_record_count,
                "chunk_count": chunk_count,
                "typename": typename,
                **workflow_args,
            },
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

    @activity.defn
    async def set_workflow_activity_context(self, workflow_id: str):
        """
        As we use a single worker thread, we need to set the workflow activity context
        """
        workflow_args = StateStore.extract_configuration(workflow_id)
        credentials = StateStore.extract_credentials(workflow_args["credential_guid"])

        if not self.sql_resource:
            self.sql_resource = SQLResource(SQLResourceConfig())

        self.sql_resource.set_credentials(credentials)
        await self.sql_resource.load()
        return workflow_args

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]):
        """
        Run the workflow.

        :param workflow_config: The workflow configuration.
        """
        workflow_id = workflow_config["workflow_id"]

        # dedicated activity to set the workflow activity context
        workflow_args: Dict[str, Any] = await workflow.execute_activity(
            self.set_workflow_activity_context,
            workflow_id,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        if not self.temporal_resource:
            self.temporal_resource = TemporalResource(
                TemporalConfig(application_name=self.application_name)
            )

        workflow_run_id = workflow.info().run_id
        workflow_args["workflow_run_id"] = workflow_run_id

        workflow.logger.info(f"Starting extraction workflow for {workflow_id}")
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )

        await workflow.execute_activity(
            self.preflight_check,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        output_prefix = workflow_args["output_prefix"]
        output_path = f"{output_prefix}/{workflow_id}/{workflow_run_id}"
        workflow_args["output_path"] = output_path

        fetch_and_transforms = [
            self.fetch_and_transform(self.fetch_databases, workflow_args, retry_policy),
            self.fetch_and_transform(self.fetch_schemas, workflow_args, retry_policy),
            self.fetch_and_transform(self.fetch_tables, workflow_args, retry_policy),
            self.fetch_and_transform(self.fetch_columns, workflow_args, retry_policy),
        ]

        await asyncio.gather(*fetch_and_transforms)

        workflow.logger.info(f"Extraction workflow completed for {workflow_id}")
