import daft
from typing import Dict, Any, List, Optional, Type
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.sql.sql import Database, Table
from application_sdk.common.logger_adaptors import get_logger
from pyatlan.model.enums import AtlanConnectorType, EntityStatus
from datetime import datetime
from application_sdk.transformers.common.utils import process_text

logger = get_logger(__name__)

class SQLTransformer(TransformerInterface):
    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        # self.schema_registry = schema_registry_client
        self.current_epoch = kwargs.get("current_epoch", "0")
        self.connector_name = connector_name
        self.tenant_id = tenant_id
        self.entity_class_definitions: Dict[str, Type[Any]] = {
            "DATABASE": Database,
            # "SCHEMA": Schema,
            "TABLE": Table,
            # "VIEW": Table,
            # "COLUMN": Column,
            # "MATERIALIZED VIEW": Table,
            # "FUNCTION": Function,
            # "TAG_REF": TagAttachment,
            # "PROCEDURE": Procedure,
        }
    
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
            enriched_df = dataframe.with_columns({
                "status": daft.lit(EntityStatus.ACTIVE),
                "tenant_id": daft.lit(self.tenant_id),
                "last_sync_workflow_name": daft.lit(workflow_id),
                "last_sync_run": daft.lit(workflow_run_id),
                "last_sync_run_at": daft.lit(datetime.now()),
                "connector_name": daft.col("connection_qualified_name").apply(
                    lambda x: AtlanConnectorType.get_connector_name(x), return_dtype=daft.DataType.string()
                ),
            })

            # Add description from remarks or comment if they exist
            if "remarks" in dataframe.column_names or "comment" in dataframe.column_names:
                enriched_df = enriched_df.with_column(
                    "description",
                    (~daft.col("remarks").is_null()).if_else(
                        daft.col("remarks"),
                        (~daft.col("comment").is_null()).if_else(
                            daft.col("comment"),
                            daft.lit(None)
                        )
                    ).apply(process_text, return_dtype=daft.DataType.string())
                )

            # Add source created by if source_owner exists
            if "source_owner" in dataframe.column_names:
                enriched_df = enriched_df.with_columns({
                    "source_created_by": daft.col("source_owner")
                })

            # Add timestamps if the columns exist
            if "created" in dataframe.column_names:
                enriched_df = enriched_df.with_column(
                    "source_created_at",
                    (~daft.col("created").is_null()).if_else(
                        daft.col("created").apply(lambda x: datetime.fromtimestamp(x / 1000), return_dtype=daft.DataType.timestamp()),
                        daft.lit(None)
                    )
                )

            if "last_altered" in dataframe.column_names:
                enriched_df = enriched_df.with_column(
                    "source_updated_at",
                    (~daft.col("last_altered").is_null()).if_else(
                        daft.col("last_altered").apply(lambda x: datetime.fromtimestamp(x / 1000), return_dtype=daft.DataType.timestamp()),
                        daft.lit(None)
                    )
                )

            # Add custom attributes if source_id exists
            if "source_id" in dataframe.column_names:
                enriched_df = enriched_df.with_columns({
                    "source_id": daft.col("source_id")
                })

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
            self.entity_class_definitions = entity_class_definitions or self.entity_class_definitions
            entity_class = self.entity_class_definitions.get(typename)
            dataframe = dataframe.with_columns(
                {
                    "connection_qualified_name": daft.lit(kwargs.get("connection_qualified_name", None)),
                    "connection_name": daft.lit(kwargs.get("connection_name", None)),
                }
            )
            if not entity_class:
                raise ValueError(f"No SQL transformation registered for {typename}")
                
            sql_template = entity_class.query
            
            transformed_df = daft.sql(sql_template)
            transformed_df = self._enrich_entity_with_metadata(
                workflow_id,
                workflow_run_id,
                transformed_df
            )
            return transformed_df
        except Exception as e:
            logger.error(f"Error transforming {typename}: {e}")
            raise e
