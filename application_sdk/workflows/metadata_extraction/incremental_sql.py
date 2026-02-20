"""Incremental SQL metadata extraction workflow implementation.

This module provides the IncrementalSQLMetadataExtractionWorkflow class that extends
BaseSQLMetadataExtractionWorkflow with incremental extraction capabilities.

The workflow implements a 4-phase execution: setup (fetch args, marker,
previous state), base extraction (databases, schemas, tables, transform),
incremental columns (prepare queries, execute batches in parallel, transform),
and finalization (write current state, update marker, upload).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities.metadata_extraction.incremental import (
    IncrementalSQLMetadataExtractionActivities,
)
from application_sdk.common.incremental.models import IncrementalWorkflowArgs
from application_sdk.constants import MAX_CONCURRENT_COLUMN_BATCHES
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows.metadata_extraction.sql import (
    BaseSQLMetadataExtractionWorkflow,
)

logger = get_logger(__name__)
workflow.logger = logger


@workflow.defn
class IncrementalSQLMetadataExtractionWorkflow(BaseSQLMetadataExtractionWorkflow):
    """Workflow for incremental SQL metadata extraction.

    This workflow extends BaseSQLMetadataExtractionWorkflow with incremental
    extraction capabilities:

    Workflow Flow:
        1. Setup: Fetch args, marker timestamp, previous current-state
        2. Base Extraction: SDK handles databases, schemas, tables, procedures
        3. Incremental Column Extraction: (if all prerequisites met)
        4. Finalization: Write current-state, update marker

    Incremental Prerequisites (all must be true):
        - incremental_extraction flag enabled
        - marker_timestamp exists from previous run
        - current_state_available (previous state uploaded)

    Attributes:
        activities_cls: The activities class to use for this workflow.
            Must be a subclass of IncrementalSQLMetadataExtractionActivities.
    """

    activities_cls: Type[IncrementalSQLMetadataExtractionActivities] = (
        IncrementalSQLMetadataExtractionActivities
    )

    @staticmethod
    def get_activities(
        activities: IncrementalSQLMetadataExtractionActivities,
    ) -> Sequence[Callable[..., Any]]:
        """Register all activities used by this workflow.

        Args:
            activities: The activities instance containing the metadata extraction
                operations.

        Returns:
            Sequence of activity methods to be executed.
        """
        return [
            activities.preflight_check,
            activities.get_workflow_args,
            activities.fetch_incremental_marker,
            activities.read_current_state,
            activities.fetch_databases,
            activities.fetch_schemas,
            activities.fetch_tables,
            activities.fetch_columns,
            activities.fetch_procedures,
            activities.transform_data,
            activities.prepare_column_extraction_queries,
            activities.execute_single_column_batch,
            activities.write_current_state,
            activities.upload_to_atlan,
            activities.update_incremental_marker,
            activities.save_workflow_state,
        ]

    async def _run_incremental_column_extraction(
        self,
        workflow_args: Dict[str, Any],
        workflow_id: str,
        workflow_run_id: str,
        retry_policy: RetryPolicy,
    ) -> None:
        """Extract columns for changed/backfill tables in parallel, then transform.

        Args:
            workflow_args: Dictionary containing workflow configuration.
            workflow_id: Unique identifier for the workflow.
            workflow_run_id: Unique identifier for this specific run.
            retry_policy: Retry policy for activity execution.
        """
        # Step 1: Prepare batched SQL queries
        prepare_result = await workflow.execute_activity_method(
            self.activities_cls.prepare_column_extraction_queries,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        total_batches = prepare_result.get("total_batches", 0)
        if total_batches == 0:
            workflow.logger.info("No tables need column extraction")
            return

        # Step 2: Execute batches with controlled concurrency
        workflow.logger.info(
            f"Executing {total_batches} column extraction batches "
            f"(max {MAX_CONCURRENT_COLUMN_BATCHES} concurrent)"
        )
        all_results: List[Dict[str, Any]] = []

        # Schedule and execute in chunks to avoid overwhelming the system
        for i in range(0, total_batches, MAX_CONCURRENT_COLUMN_BATCHES):
            chunk = list(
                range(i, min(i + MAX_CONCURRENT_COLUMN_BATCHES, total_batches))
            )

            handles = [
                workflow.start_activity_method(
                    self.activities_cls.execute_single_column_batch,
                    {
                        **workflow_args,
                        "batch_index": idx,
                        "total_batches": total_batches,
                    },
                    retry_policy=retry_policy,
                    start_to_close_timeout=self.default_start_to_close_timeout,
                    heartbeat_timeout=self.default_heartbeat_timeout,
                )
                for idx in chunk
            ]

            # Wait for this chunk to complete before scheduling next
            all_results.extend(await asyncio.gather(*handles))

        total_records = sum(
            r.get("records", 0) for r in all_results if isinstance(r, dict)
        )
        workflow.logger.info(
            f"Column extraction complete: {total_batches} batches, {total_records} records"
        )

        # Step 3: Transform extracted columns
        if total_records > 0:
            transform_args: Dict[str, Any] = {
                **workflow_args,
                "typename": "column",
                "workflow_id": workflow_id,
                "workflow_run_id": workflow_run_id,
            }
            transform_args.pop("file_names", None)

            if not transform_args.get("connection", {}).get(
                "connection_qualified_name"
            ):
                raise ValueError(
                    "connection_qualified_name required for column transformation"
                )

            await workflow.execute_activity_method(
                self.activities_cls.transform_data,
                transform_args,
                retry_policy=retry_policy,
                start_to_close_timeout=self.default_start_to_close_timeout,
                heartbeat_timeout=self.default_heartbeat_timeout,
            )
            workflow.logger.info("Column transformation complete")

    # ════════════════════════════════════════════════════════════════════════════
    # MAIN WORKFLOW ENTRY POINT
    # ════════════════════════════════════════════════════════════════════════════

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        """Execute the incremental SQL metadata extraction workflow.

        This method orchestrates the 4-phase incremental extraction process:
        1. Setup: Fetch args, marker, and previous current-state
        2. Base Extraction: Run standard SQL metadata extraction
        3. Incremental Column Extraction: Extract columns for changed tables only
        4. Finalization: Write new current-state and update marker

        Args:
            workflow_config: Dictionary containing workflow_id and other parameters.
        """
        workflow_id = workflow_config["workflow_id"]
        workflow_run_id = workflow.info().run_id
        retry_policy = RetryPolicy(maximum_attempts=3, backoff_coefficient=2)

        # ── PHASE 1: Setup - Fetch args and incremental prerequisites ──────────
        workflow_args = await workflow.execute_activity_method(
            self.activities_cls.get_workflow_args,
            workflow_config,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        # Inject workflow_run_id so activities (e.g. write_current_state) can
        # build run-specific S3 paths like incremental-diff/{run_id}/
        workflow_args["workflow_run_id"] = workflow_run_id

        workflow_args = await workflow.execute_activity_method(
            self.activities_cls.fetch_incremental_marker,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        workflow_args = await workflow.execute_activity_method(
            self.activities_cls.read_current_state,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        await workflow.execute_activity_method(
            self.activities_cls.save_workflow_state,
            args=[workflow_id, workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        # ── PHASE 2: Base Extraction (SDK handles fetch + transform) ───────────
        # Note: super().run() calls get_workflow_args internally, which reads our
        # saved state from StateStore (including marker_timestamp and current_state).
        await super().run(workflow_config)

        # ── PHASE 3: Incremental Column Extraction (if prerequisites met) ──────
        args = IncrementalWorkflowArgs.model_validate(workflow_args)
        if args.is_incremental_ready():
            workflow.logger.info(
                f"Running incremental column extraction: "
                f"marker={bool(args.metadata.marker_timestamp)}, "
                f"state={args.metadata.current_state_available}"
            )
            await self._run_incremental_column_extraction(
                workflow_args, workflow_id, workflow_run_id, retry_policy
            )
        else:
            workflow.logger.info(
                "Skipping incremental extraction: prerequisites not met"
            )

        # ── PHASE 4: Finalization - Write state, update marker ─────────────────
        workflow_args = await workflow.execute_activity_method(
            self.activities_cls.write_current_state,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        await workflow.execute_activity_method(
            self.activities_cls.update_incremental_marker,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        await self.run_exit_activities(workflow_args)
