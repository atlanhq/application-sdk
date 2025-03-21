from typing import Any, Dict, List, Optional

import daft
from temporalio import activity

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.transformers.atlas import AtlasTransformer

activity.logger = get_logger(__name__)


@daft.udf(
    # Specify the compute resource requirements for the UDF
    # memory_bytes=524288000,
    # num_cpus=1,
    # num_gpus=1,
    # batch_size=100000,
    return_dtype=daft.DataType.python()
)
def process_rows(
    *dataframe_columns,
    typename: str,
    workflow_id: str,
    workflow_run_id: str,
    connection_name: Optional[str],
    connection_qualified_name: Optional[str],
    transformer: AtlasTransformer,
) -> List[Optional[Dict[str, Any]]]:
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
    transformed_metadata_list = []
    column_names, data = zip(
        *[(col.name(), col.to_pylist()) for col in dataframe_columns]
    )
    rows = [dict(zip(column_names, row)) for row in zip(*data)]
    for row in rows:
        try:
            transformed_metadata: Optional[Dict[str, Any]] = (
                transformer.transform_metadata(
                    typename,
                    row,
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run_id,
                    connection_name=connection_name,
                    connection_qualified_name=connection_qualified_name,
                )
            )
            if transformed_metadata:
                transformed_metadata_list.append(transformed_metadata)
            else:
                activity.logger.warning(f"Skipped invalid {typename} data: {row}")
        except Exception as row_error:
            activity.logger.error(f"Error processing row for {typename}: {row_error}")
    return transformed_metadata_list
