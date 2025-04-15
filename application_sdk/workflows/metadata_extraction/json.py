"""JSON metadata extraction workflow implementation.

This module implements the JSON metadata extraction workflow using Temporal.
"""

from typing import Any, Dict, List, Optional, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities import ActivitiesInterface
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.error_codes import ApplicationFrameworkErrorCodes
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)

class JSONMetadataExtractionWorkflow(WorkflowInterface):
    def __init__(self, activities: ActivitiesInterface, state_store: StateStoreInput):
        self.activities = activities
        self.state_store = state_store

    async def execute(self, input_data: Any) -> Any:
        try:
            # Implementation of the workflow
            return await self.activities.process_json_metadata(input_data)
        except Exception as e:
            logger.error(
                f"JSON metadata extraction workflow failed: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.WorkflowErrorCodes.JSON_METADATA_EXTRACTION_ERROR,
                exc_info=True
            )
            raise 