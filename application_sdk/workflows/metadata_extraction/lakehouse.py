"""Lakehouse workflow mixin — raw data loading into Iceberg via MDLH.

Provides methods that MetadataExtractionWorkflow calls to load raw data into
the lakehouse.  All env-var checks and MDLH interaction are encapsulated here.
"""

from typing import Any, Dict, List

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.constants import (
    ENABLE_LAKEHOUSE_LOAD,
    LH_LOAD_RAW_MODE,
    LH_LOAD_RAW_NAMESPACE,
    LH_LOAD_RAW_TABLE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class LakehouseLoadMixin:
    """Mixin providing lakehouse loading methods for metadata extraction workflows.

    Requires the host class to have:
      - self.activities_cls  (with load_to_lakehouse, prepare_raw_for_lakehouse methods)
      - self.default_start_to_close_timeout
      - self.default_heartbeat_timeout
    """

    async def load_raw_to_lakehouse(
        self,
        workflow_args: Dict[str, Any],
        extracted_typenames: List[str],
    ) -> None:
        """Prepare raw parquet and load into the per-connector raw table.

        Enriches raw parquet files with metadata columns (+ raw_record as
        JSON string), then loads into int_entity_raw.{APPLICATION_NAME}
        via MDLH /load API.

        No-op if lakehouse loading is disabled or no typenames were extracted.
        """
        if not (
            ENABLE_LAKEHOUSE_LOAD
            and LH_LOAD_RAW_NAMESPACE
            and LH_LOAD_RAW_TABLE_NAME
            and extracted_typenames
        ):
            logger.info("Lakehouse load (raw) skipped")
            return

        # Step 1: Enrich raw parquet with metadata columns
        # The activity reads output_path, workflow_id, workflow_run_id,
        # connection info directly from workflow_args.
        # Shallow copy to avoid mutating the shared workflow_args dict.
        prepare_args = {**workflow_args, "_extracted_typenames": extracted_typenames}
        raw_lh_dir = await workflow.execute_activity_method(
            self.activities_cls.prepare_raw_for_lakehouse,
            args=[prepare_args],
            retry_policy=RetryPolicy(maximum_attempts=6, backoff_coefficient=2),
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        # Step 2: Load parquet into int_entity_raw.{connector}
        logger.info(
            f"Loading raw data into {LH_LOAD_RAW_NAMESPACE}.{LH_LOAD_RAW_TABLE_NAME}"
        )
        await self._submit_lakehouse_load(
            output_path=raw_lh_dir,
            namespace=LH_LOAD_RAW_NAMESPACE,
            table_name=LH_LOAD_RAW_TABLE_NAME,
            mode=LH_LOAD_RAW_MODE,
            file_extension=".parquet",
        )

    async def _submit_lakehouse_load(
        self,
        output_path: str,
        namespace: str,
        table_name: str,
        mode: str,
        file_extension: str,
    ) -> None:
        """Submit a single MDLH /load job and poll until completion."""
        load_config = {
            "lh_load_config": {
                "output_path": output_path,
                "namespace": namespace,
                "table_name": table_name,
                "mode": mode,
                "file_extension": file_extension,
            },
        }
        await workflow.execute_activity_method(
            self.activities_cls.load_to_lakehouse,
            args=[load_config],
            retry_policy=RetryPolicy(maximum_attempts=6, backoff_coefficient=2),
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )
