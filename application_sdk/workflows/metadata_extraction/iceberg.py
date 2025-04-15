"""Iceberg metadata extraction workflow implementation.

This module implements the Iceberg metadata extraction workflow using Temporal.
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

# ... existing code ...

        except Exception as e:
            logger.error(
                f"Iceberg metadata extraction workflow failed: {str(e)}",
                error_code=ApplicationFrameworkErrorCodes.WorkflowErrorCodes.ICEBERG_METADATA_EXTRACTION_ERROR,
                exc_info=True
            )
            raise 