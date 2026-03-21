from typing import Any, Dict, List

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.constants import (
    ENABLE_ATLAN_UPLOAD,
    ENABLE_LAKEHOUSE_LOAD,
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


def _resolve_iceberg_table(typename: str) -> str:
    """Resolve SDK typename to Iceberg table name in entity_metadata.

    MDLH tables follow the convention: lowercase(AtlasTypeDef).
    Most SDK typenames already match (e.g. "database", "table", "column",
    "lookerdashboard", "snowflakedynamictable").  Overrides exist only for
    SDK-specific naming quirks like "extras-procedure" → "procedure".
    """
    return _TYPENAME_OVERRIDES.get(typename, typename.lower())


class MetadataExtractionWorkflow(WorkflowInterface):
    """Base workflow for metadata extraction."""

    async def _execute_lakehouse_load(
        self,
        workflow_args: Dict[str, Any],
        output_path: str,
        namespace: str,
        table_name: str,
        mode: str,
        file_extension: str,
    ) -> None:
        """Execute a single lakehouse load activity call."""
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
        retry_policy = RetryPolicy(maximum_attempts=6, backoff_coefficient=2)
        await workflow.execute_activity_method(
            self.activities_cls.load_to_lakehouse,
            args=[load_config],
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

    async def _load_transformed_to_lakehouse(
        self,
        workflow_args: Dict[str, Any],
    ) -> None:
        """Load transformed data to lakehouse, one MDLH job per entity type.

        Each typename produced during extraction is loaded into its own
        Iceberg table inside entity_metadata.  The table name is derived
        from the typename via _resolve_iceberg_table (defaults to
        typename.lower(), matching MDLH's naming convention).
        """
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
            await self._execute_lakehouse_load(
                workflow_args,
                output_path=f"{output_path}/transformed/{typename}",
                namespace=LH_LOAD_TRANSFORMED_NAMESPACE,
                table_name=iceberg_table,
                mode=LH_LOAD_TRANSFORMED_MODE,
                file_extension=".jsonl",
            )

    async def run_exit_activities(self, workflow_args: Dict[str, Any]) -> None:
        """Run the exit activities for the workflow."""
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )
        if ENABLE_ATLAN_UPLOAD:
            workflow_args["typename"] = "atlan-upload"
            await workflow.execute_activity_method(
                self.activities_cls.upload_to_atlan,
                args=[workflow_args],
                retry_policy=retry_policy,
                start_to_close_timeout=self.default_start_to_close_timeout,
                heartbeat_timeout=self.default_heartbeat_timeout,
            )
        else:
            logger.info("Atlan upload skipped for workflow (disabled)")

        if ENABLE_LAKEHOUSE_LOAD and LH_LOAD_TRANSFORMED_NAMESPACE:
            await self._load_transformed_to_lakehouse(workflow_args)
        else:
            logger.info("Lakehouse load (transformed) skipped for workflow (disabled)")
