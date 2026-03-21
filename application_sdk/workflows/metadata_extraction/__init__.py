from time import time
from typing import Any, Dict, List

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.constants import (
    APP_TENANT_ID,
    ENABLE_ATLAN_UPLOAD,
    ENABLE_LAKEHOUSE_LOAD,
    LH_LOAD_RAW_MODE,
    LH_LOAD_RAW_NAMESPACE,
    LH_LOAD_RAW_TABLE_NAME,
    LH_LOAD_TRANSFORMED_MODE,
    LH_LOAD_TRANSFORMED_NAMESPACE,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)

# SDK typenames that don't match the MDLH Iceberg table name directly.
# Default behaviour: table_name = typename.lower()
# Override here only when the SDK typename differs from the Atlas typedef.
_TYPENAME_OVERRIDES: Dict[str, str] = {
    "extras-procedure": "procedure",
}

_DEFAULT_RETRY_POLICY = RetryPolicy(maximum_attempts=6, backoff_coefficient=2)


def _resolve_iceberg_table(typename: str) -> str:
    """Resolve SDK typename to Iceberg table name in entity_metadata.

    MDLH tables follow the convention: lowercase(AtlasTypeDef).
    Most SDK typenames already match (e.g. "database", "table", "column",
    "lookerdashboard", "snowflakedynamictable").  Overrides exist only for
    SDK-specific naming quirks like "extras-procedure" -> "procedure".
    """
    return _TYPENAME_OVERRIDES.get(typename, typename.lower())


class MetadataExtractionWorkflow(WorkflowInterface):
    """Base workflow for metadata extraction.

    Lakehouse loading is built in and controlled via environment variables.
    When ENABLE_LAKEHOUSE_LOAD is true:
      - load_raw_to_lakehouse: parquet -> common-schema JSONL -> entity_raw.{connector}
      - load_transformed_to_lakehouse: per-typename JSONL -> entity_metadata.{typename}
    """

    # ---- Lakehouse loading (public API for subclasses) ----

    async def load_raw_to_lakehouse(
        self,
        workflow_args: Dict[str, Any],
        extracted_typenames: List[str],
    ) -> None:
        """Prepare raw parquet and load into the per-connector raw table.

        Converts raw parquet files into common-schema JSONL (with metadata
        columns + raw_record as JSON string), then loads into
        entity_raw.{APPLICATION_NAME} via MDLH /load API.

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

        output_path = workflow_args.get("output_path", "")
        connection_qn = workflow_args.get("connection", {}).get(
            "connection_qualified_name", ""
        )

        # Step 1: Prepare raw parquet -> common-schema JSONL
        prep_config = {
            **workflow_args,
            "raw_lakehouse_config": {
                "raw_output_path": f"{output_path}/raw",
                "typenames": extracted_typenames,
                "connection_qualified_name": connection_qn,
                "workflow_id": workflow_args.get("workflow_id", ""),
                "workflow_run_id": workflow_args.get("workflow_run_id", ""),
                "extracted_at": int(time() * 1000),
                "tenant_id": APP_TENANT_ID,
            },
        }
        raw_lh_dir = await workflow.execute_activity_method(
            self.activities_cls.prepare_raw_for_lakehouse,
            args=[prep_config],
            retry_policy=_DEFAULT_RETRY_POLICY,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        # Step 2: Load JSONL into entity_raw.{connector}
        logger.info(
            f"Loading raw data into {LH_LOAD_RAW_NAMESPACE}.{LH_LOAD_RAW_TABLE_NAME}"
        )
        await self._submit_lakehouse_load(
            workflow_args,
            output_path=raw_lh_dir,
            namespace=LH_LOAD_RAW_NAMESPACE,
            table_name=LH_LOAD_RAW_TABLE_NAME,
            mode=LH_LOAD_RAW_MODE,
            file_extension=".jsonl",
        )

    async def load_transformed_to_lakehouse(
        self,
        workflow_args: Dict[str, Any],
    ) -> None:
        """Load transformed data into per-entity-type Iceberg tables.

        For each typename produced during extraction, loads the transformed
        JSONL files into entity_metadata.{typename.lower()} via MDLH /load API.

        No-op if lakehouse loading is disabled or no typenames were extracted.
        """
        if not (ENABLE_LAKEHOUSE_LOAD and LH_LOAD_TRANSFORMED_NAMESPACE):
            logger.info("Lakehouse load (transformed) skipped")
            return

        typenames: List[str] = workflow_args.get("_extracted_typenames", [])
        if not typenames:
            logger.info("No typenames extracted, skipping lakehouse load (transformed)")
            return

        output_path = workflow_args.get("output_path", "")

        for typename in typenames:
            iceberg_table = _resolve_iceberg_table(typename)
            logger.info(
                f"Loading transformed data for typename={typename} "
                f"into {LH_LOAD_TRANSFORMED_NAMESPACE}.{iceberg_table}"
            )
            await self._submit_lakehouse_load(
                workflow_args,
                output_path=f"{output_path}/transformed/{typename}",
                namespace=LH_LOAD_TRANSFORMED_NAMESPACE,
                table_name=iceberg_table,
                mode=LH_LOAD_TRANSFORMED_MODE,
                file_extension=".jsonl",
            )

    # ---- Exit activities ----

    async def run_exit_activities(self, workflow_args: Dict[str, Any]) -> None:
        """Run post-extraction activities: upload to Atlan + lakehouse load."""
        if ENABLE_ATLAN_UPLOAD:
            workflow_args["typename"] = "atlan-upload"
            await workflow.execute_activity_method(
                self.activities_cls.upload_to_atlan,
                args=[workflow_args],
                retry_policy=_DEFAULT_RETRY_POLICY,
                start_to_close_timeout=self.default_start_to_close_timeout,
                heartbeat_timeout=self.default_heartbeat_timeout,
            )
        else:
            logger.info("Atlan upload skipped for workflow (disabled)")

        await self.load_transformed_to_lakehouse(workflow_args)

    # ---- Private helpers ----

    async def _submit_lakehouse_load(
        self,
        workflow_args: Dict[str, Any],
        output_path: str,
        namespace: str,
        table_name: str,
        mode: str,
        file_extension: str,
    ) -> None:
        """Submit a single MDLH /load job and poll until completion."""
        load_config = {
            **workflow_args,
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
            retry_policy=_DEFAULT_RETRY_POLICY,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )
