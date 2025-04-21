import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Type

import daft
from pyatlan.model.enums import AtlanConnectorType, EntityStatus

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.common.utils import process_text

logger = get_logger(__name__)

import yaml


class SQLTransformer(TransformerInterface):
    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        # self.schema_registry = schema_registry_client
        self.current_epoch = kwargs.get("current_epoch", "0")
        self.connector_name = connector_name
        self.tenant_id = tenant_id
        self.entity_class_definitions: Dict[str, str] = (
            self._generate_detault_yaml_mappings(entities=["DATABASE", "TABLE"])
        )

    def _generate_detault_yaml_mappings(
        self,
        entities: List[str],
        base_path: str = f"{os.path.dirname(__file__)}/sql_query_templates",
    ) -> Dict[str, str]:
        return {
            entity: os.path.join(base_path, f"{entity.lower()}.yaml")
            for entity in entities
        }

    def _handle_column_name(self, column_name: str) -> str:
        """Handle column names that contain dots by quoting them.

        Args:
            column_name: The column name to process

        Returns:
            The processed column name, quoted if it contains dots
        """
        if "." in column_name:
            return f'"{column_name}"'
        return column_name

    def _process_column(self, column: Dict[str, str]) -> str:
        """Process a single column definition into a SQL column expression.

        Args:
            column: The column definition dictionary

        Returns:
            A SQL column expression string
        """
        column["name"] = self._handle_column_name(column["name"])
        return f"{column['source']} AS {column['name']}"

    def _get_columns(
        self, sql_template: Dict[str, Any], dataframe: daft.DataFrame
    ) -> List[str]:
        """Get the columns for the SQL query.

        Args:
            dataframe: The DataFrame to get columns from

        Returns:
            A list of column expressions for the SQL query
        """
        columns: List[Dict[str, str]] = []
        for column in sql_template["columns"]:
            if column.get("source_columns") and all(
                col in dataframe.column_names for col in column["source_columns"]
            ):
                columns.append(self._process_column(column))
            elif column["source"] in dataframe.column_names:
                columns.append(self._process_column(column))
            elif (
                column["source"].startswith("'")
                and column["source"].endswith("'")
                and len(column["source"]) > 1
            ):
                # This is a string literal and should be added as is
                columns.append(self._process_column(column))

        return columns

    def generate_query(self, yaml_path: str, dataframe: daft.DataFrame) -> str:
        with open(yaml_path, "r") as f:
            sql_template = yaml.safe_load(f)
        columns = self._get_columns(sql_template, dataframe)

        return f"""
        SELECT
            {','.join(columns)}
        FROM
            dataframe
        """

    def _group_columns_by_prefix(self, dataframe: daft.DataFrame) -> daft.DataFrame:
        """Group columns with the same prefix into structs.

        Args:
            dataframe (daft.DataFrame): DataFrame to restructure

        Returns:
            daft.DataFrame: DataFrame with columns grouped into structs
        """
        # Get all column names
        columns = dataframe.column_names

        # Group columns by prefix
        prefix_groups = {}
        standalone_columns = []

        for col in columns:
            if "." in col:
                prefix, suffix = col.split(".", 1)
                if prefix not in prefix_groups:
                    prefix_groups[prefix] = []
                prefix_groups[prefix].append((col, suffix))
            else:
                standalone_columns.append(col)

        # Create new DataFrame with restructured columns
        new_columns = []

        # Add standalone columns as is
        for col in standalone_columns:
            new_columns.append(daft.col(col))

        # Create structs for prefixed columns
        for prefix, columns in prefix_groups.items():
            # Create a dictionary of field expressions
            struct_fields = (
                daft.col(full_col).alias(suffix) for full_col, suffix in columns
            )
            # Create the struct expression
            new_columns.append(daft.struct(*struct_fields).alias(prefix))

        return dataframe.select(*new_columns)

    def _enrich_entity_with_metadata(
        self,
        workflow_id: str,
        workflow_run_id: str,
        dataframe: daft.DataFrame,
    ) -> daft.DataFrame:
        """Enrich a DataFrame with additional metadata.

        This method adds workflow metadata and other attributes to the DataFrame.

        Args:
            workflow_id (str): ID of the workflow.
            workflow_run_id (str): ID of the workflow run.
            dataframe (daft.DataFrame): DataFrame to enrich.

        Returns:
            daft.DataFrame: The enriched DataFrame.
        """
        try:
            # Add standard attributes
            enriched_df = dataframe.with_columns(
                {
                    "status": daft.lit(EntityStatus.ACTIVE),
                    "attributes.tenantId": daft.lit(self.tenant_id),
                    "attributes.lastSyncWorkflowName": daft.lit(workflow_id),
                    "attributes.lastSyncRun": daft.lit(workflow_run_id),
                    "attributes.lastSyncRunAt": daft.lit(datetime.now()),
                    "attributes.connectorName": daft.col(
                        "attributes.connectionQualifiedName"
                    ).apply(
                        lambda x: AtlanConnectorType.get_connector_name(x),
                        return_dtype=daft.DataType.string(),
                    ),
                }
            )
            daft.DataType
            # Add description from remarks or comment if they exist
            if (
                "attributes.remarks" in dataframe.column_names
                or "attributes.comment" in dataframe.column_names
            ):
                enriched_df = enriched_df.with_column(
                    "attributes.description",
                    (~daft.col("attributes.remarks").is_null())
                    .if_else(
                        daft.col("attributes.remarks"),
                        (~daft.col("attributes.comment").is_null()).if_else(
                            daft.col("attributes.comment"), daft.lit(None)
                        ),
                    )
                    .apply(process_text, return_dtype=daft.DataType.string()),
                )

            # Add source created by if source_owner exists
            if "custom_attributes.source_owner" in dataframe.column_names:
                enriched_df = enriched_df.with_columns(
                    {
                        "attributes.sourceCreatedBy": daft.col(
                            "custom_attributes.source_owner"
                        )
                    }
                )

            # Add timestamps if the columns exist
            if "custom_attributes.created" in dataframe.column_names:
                enriched_df = enriched_df.with_column(
                    "attributes.sourceCreatedAt",
                    (~daft.col("custom_attributes.created").is_null()).if_else(
                        daft.col("custom_attributes.created").apply(
                            lambda x: datetime.fromtimestamp(x / 1000),
                            return_dtype=daft.DataType.timestamp(),
                        ),
                        daft.lit(None),
                    ),
                )

            if "custom_attributes.last_altered" in dataframe.column_names:
                enriched_df = enriched_df.with_column(
                    "attributes.sourceUpdatedAt",
                    (~daft.col("custom_attributes.last_altered").is_null()).if_else(
                        daft.col("custom_attributes.last_altered").apply(
                            lambda x: datetime.fromtimestamp(x / 1000),
                            return_dtype=daft.DataType.timestamp(),
                        ),
                        daft.lit(None),
                    ),
                )

            # Add custom attributes if source_id exists
            if "custom_attributes.source_id" in dataframe.column_names:
                enriched_df = enriched_df.with_columns(
                    {"attributes.sourceId": daft.col("custom_attributes.source_id")}
                )

            # Group columns by prefix into structs
            enriched_df = self._group_columns_by_prefix(enriched_df)

            return enriched_df
        except Exception as e:
            logger.error(f"Error enriching DataFrame with metadata: {e}")
            raise e

    def transform_metadata(
        self,
        typename: str,
        dataframe: daft.DataFrame,
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: Dict[str, Type[Any]] | None = None,
        **kwargs: Any,
    ) -> Optional[daft.DataFrame]:
        """Transform records using SQL executed through Daft"""
        try:
            if dataframe.count_rows() == 0:
                return None

            typename = typename.upper()
            self.entity_class_definitions = (
                entity_class_definitions or self.entity_class_definitions
            )
            entity_sql_template_path = self.entity_class_definitions.get(typename)
            if not entity_sql_template_path:
                raise ValueError(f"No SQL transformation registered for {typename}")

            dataframe = dataframe.with_columns(
                {
                    "connection_qualified_name": daft.lit(
                        kwargs.get("connection_qualified_name", None)
                    ),
                    "connection_name": daft.lit(kwargs.get("connection_name", None)),
                }
            )

            entity_sql_template = self.generate_query(
                entity_sql_template_path, dataframe
            )
            transformed_df = daft.sql(entity_sql_template)
            transformed_df = self._enrich_entity_with_metadata(
                workflow_id, workflow_run_id, transformed_df
            )
            return transformed_df
        except Exception as e:
            logger.error(f"Error transforming {typename}: {e}")
            raise e
