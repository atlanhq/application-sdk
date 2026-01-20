"""Incremental SQL metadata extraction workflow mixin.

This module provides the IncrementalSQLWorkflowMixin class that can be composed
with BaseSQLMetadataExtractionWorkflow to add incremental extraction capabilities.

Approach A: Composable Activities with SDK Workflow Helpers
- Apps compose workflow: `class MyWorkflow(BaseSQLMetadataExtractionWorkflow, IncrementalSQLWorkflowMixin)`
- Provides helper methods for 4-phase orchestration
- Apps can use default `run_incremental_extraction()` or compose phases manually
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.common.incremental.constants import MAX_CONCURRENT_COLUMN_BATCHES
from application_sdk.common.incremental.models import WorkflowMetadata
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class IncrementalSQLWorkflowMixin:
    """Mixin providing incremental extraction workflow helpers.

    This mixin adds incremental extraction orchestration to SQL metadata extraction
    workflows. Compose with BaseSQLMetadataExtractionWorkflow:

        @workflow.defn
        class OracleWorkflow(BaseSQLMetadataExtractionWorkflow, IncrementalSQLWorkflowMixin):
            activities_cls = OracleActivities

            @workflow.run
            async def run(self, workflow_config: Dict[str, Any]) -> None:
                # Use SDK-provided default 4-phase flow
                await self.run_incremental_extraction(workflow_config)

    Or compose phases manually:

        @workflow.defn
        class OracleWorkflowCustom(BaseSQLMetadataExtractionWorkflow, IncrementalSQLWorkflowMixin):
            activities_cls = OracleActivities

            @workflow.run
            async def run(self, workflow_config: Dict[str, Any]) -> None:
                workflow_args = await self.run_incremental_setup(workflow_config)
                await self.run_base_extraction(workflow_args)
                if self.is_incremental_ready(workflow_args):
                    await self.run_incremental_columns(workflow_args)
                await self.run_incremental_finalization(workflow_args)

    Methods provided:
    - run_incremental_extraction(): Complete 4-phase flow
    - run_incremental_setup(): Phase 1 - Setup marker and state
    - run_base_extraction(): Phase 2 - Database/schema/table extraction
    - run_incremental_columns(): Phase 3 - Batched column extraction
    - run_incremental_finalization(): Phase 4 - State update and upload
    - is_incremental_ready(): Check if prerequisites are met
    - is_incremental_enabled(): Check if feature is enabled
    """

    # Activity class (should be set by subclass)
    activities_cls: Type[Any] = None

    # Configuration
    max_concurrent_column_batches: int = MAX_CONCURRENT_COLUMN_BATCHES

    def _get_activity_options(self, timeout_minutes: int = 60) -> Dict[str, Any]:
        """Get common activity execution options."""
        return {
            "start_to_close_timeout": timedelta(minutes=timeout_minutes),
            "retry_policy": RetryPolicy(
                initial_interval=timedelta(seconds=10),
                maximum_interval=timedelta(minutes=5),
                backoff_coefficient=2.0,
                maximum_attempts=3,
            ),
        }

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def is_incremental_ready(self, workflow_args: Dict[str, Any]) -> bool:
        """Check if all prerequisites for incremental extraction are met.

        All three conditions must be true:
        - incremental_extraction is enabled
        - marker_timestamp exists
        - current_state_available is true

        Args:
            workflow_args: Workflow arguments

        Returns:
            True if incremental extraction should run
        """
        args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))
        return args.is_incremental_ready()

    def is_incremental_enabled(self, workflow_args: Dict[str, Any]) -> bool:
        """Check if incremental extraction is enabled in config.

        Args:
            workflow_args: Workflow arguments

        Returns:
            True if incremental extraction is enabled
        """
        args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))
        return args.incremental_extraction

    # =========================================================================
    # Phase 1: Setup
    # =========================================================================

    async def run_incremental_setup(
        self, workflow_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Phase 1: Setup workflow arguments, marker, and state.

        Executes:
        1. get_workflow_args (from base class)
        2. fetch_incremental_marker
        3. read_current_state

        Args:
            workflow_config: Initial workflow configuration

        Returns:
            Updated workflow_args with marker and state info
        """
        if not hasattr(self, "activities_cls") or self.activities_cls is None:
            raise ValueError("activities_cls must be set")

        activities = self.activities_cls()

        # Get workflow args (from base class method)
        workflow_args = await workflow.execute_activity_method(
            activities.get_workflow_args,
            workflow_config,
            **self._get_activity_options(timeout_minutes=30),
        )

        # Fetch incremental marker (if enabled)
        workflow_args = await workflow.execute_activity_method(
            activities.fetch_incremental_marker,
            workflow_args,
            **self._get_activity_options(timeout_minutes=10),
        )

        # Read current state (if enabled)
        workflow_args = await workflow.execute_activity_method(
            activities.read_current_state,
            workflow_args,
            **self._get_activity_options(timeout_minutes=30),
        )

        logger.info(
            f"Incremental setup complete: "
            f"enabled={self.is_incremental_enabled(workflow_args)}, "
            f"ready={self.is_incremental_ready(workflow_args)}"
        )

        return workflow_args

    # =========================================================================
    # Phase 2: Base Extraction
    # =========================================================================

    async def run_base_extraction(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        """Phase 2: Extract databases, schemas, and tables.

        Executes:
        1. fetch_databases
        2. fetch_schemas
        3. fetch_tables (uses incremental SQL if is_incremental_ready)
        4. fetch_columns (skipped if is_incremental_ready - handled in Phase 3)
        5. fetch_procedures (optional)
        6. transform_data

        Args:
            workflow_args: Workflow arguments

        Returns:
            Updated workflow_args
        """
        if not hasattr(self, "activities_cls") or self.activities_cls is None:
            raise ValueError("activities_cls must be set")

        activities = self.activities_cls()
        incremental_ready = self.is_incremental_ready(workflow_args)

        # Database extraction
        await workflow.execute_activity_method(
            activities.fetch_databases,
            workflow_args,
            **self._get_activity_options(timeout_minutes=30),
        )

        # Schema extraction
        await workflow.execute_activity_method(
            activities.fetch_schemas,
            workflow_args,
            **self._get_activity_options(timeout_minutes=60),
        )

        # Table extraction (uses incremental SQL if ready)
        await workflow.execute_activity_method(
            activities.fetch_tables,
            workflow_args,
            **self._get_activity_options(timeout_minutes=120),
        )

        # Column extraction (skipped for incremental - handled in Phase 3)
        if not incremental_ready:
            await workflow.execute_activity_method(
                activities.fetch_columns,
                workflow_args,
                **self._get_activity_options(timeout_minutes=180),
            )
        else:
            logger.info("Skipping generic column extraction - will use batched extraction")

        # Procedures (optional, may not exist in all databases)
        if hasattr(activities, "fetch_procedures"):
            try:
                await workflow.execute_activity_method(
                    activities.fetch_procedures,
                    workflow_args,
                    **self._get_activity_options(timeout_minutes=60),
                )
            except Exception as e:
                logger.warning(f"fetch_procedures failed (may be unsupported): {e}")

        # Transform data
        await workflow.execute_activity_method(
            activities.transform_data,
            workflow_args,
            **self._get_activity_options(timeout_minutes=120),
        )

        logger.info("Base extraction complete")
        return workflow_args

    # =========================================================================
    # Phase 3: Incremental Column Extraction
    # =========================================================================

    async def run_incremental_columns(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Phase 3: Extract columns for CREATED/UPDATED/BACKFILL tables only.

        Only runs if is_incremental_ready() returns True.

        Executes:
        1. prepare_column_extraction_queries
        2. execute_single_column_batch (parallel, up to max_concurrent_column_batches)
        3. transform_data (for new column batches)

        Args:
            workflow_args: Workflow arguments

        Returns:
            Updated workflow_args
        """
        if not self.is_incremental_ready(workflow_args):
            logger.info("Not incremental-ready - skipping incremental column extraction")
            return workflow_args

        if not hasattr(self, "activities_cls") or self.activities_cls is None:
            raise ValueError("activities_cls must be set")

        activities = self.activities_cls()

        # Prepare batched queries
        prepare_result = await workflow.execute_activity_method(
            activities.prepare_column_extraction_queries,
            workflow_args,
            **self._get_activity_options(timeout_minutes=60),
        )

        query_files: List[str] = prepare_result.get("query_files", [])

        if not query_files:
            logger.info("No column extraction queries to execute")
            return workflow_args

        logger.info(
            f"Executing {len(query_files)} column batches "
            f"(max concurrent: {self.max_concurrent_column_batches})"
        )

        # Execute batches in parallel with concurrency limit
        semaphore = asyncio.Semaphore(self.max_concurrent_column_batches)

        async def execute_batch(query_file: str, batch_index: int):
            async with semaphore:
                return await workflow.execute_activity_method(
                    activities.execute_single_column_batch,
                    workflow_args,
                    query_file,
                    batch_index,
                    **self._get_activity_options(timeout_minutes=120),
                )

        # Create tasks for all batches
        tasks = [
            execute_batch(query_file, idx)
            for idx, query_file in enumerate(query_files)
        ]

        # Wait for all batches to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Check for errors
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            logger.error(f"{len(errors)} column batches failed: {errors[0]}")
            raise errors[0]

        logger.info(f"All {len(query_files)} column batches completed")

        # Transform the new column data
        await workflow.execute_activity_method(
            activities.transform_data,
            workflow_args,
            **self._get_activity_options(timeout_minutes=120),
        )

        return workflow_args

    # =========================================================================
    # Phase 4: Finalization
    # =========================================================================

    async def run_incremental_finalization(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Phase 4: Finalize extraction - write state, update marker, upload.

        Only write_current_state and update_incremental_marker run if
        incremental_extraction is enabled.

        Executes:
        1. write_current_state (if incremental enabled)
        2. update_incremental_marker (if incremental enabled)
        3. upload_output

        Args:
            workflow_args: Workflow arguments

        Returns:
            Updated workflow_args
        """
        if not hasattr(self, "activities_cls") or self.activities_cls is None:
            raise ValueError("activities_cls must be set")

        activities = self.activities_cls()
        incremental_enabled = self.is_incremental_enabled(workflow_args)

        # Write current state (only if incremental enabled)
        if incremental_enabled:
            workflow_args = await workflow.execute_activity_method(
                activities.write_current_state,
                workflow_args,
                **self._get_activity_options(timeout_minutes=60),
            )

            # Update marker
            await workflow.execute_activity_method(
                activities.update_incremental_marker,
                workflow_args,
                **self._get_activity_options(timeout_minutes=10),
            )
        else:
            logger.info("Incremental disabled - skipping state and marker update")

        # Upload output (always runs)
        if hasattr(activities, "upload_output"):
            await workflow.execute_activity_method(
                activities.upload_output,
                workflow_args,
                **self._get_activity_options(timeout_minutes=60),
            )

        logger.info("Finalization complete")
        return workflow_args

    # =========================================================================
    # Complete Flow
    # =========================================================================

    async def run_incremental_extraction(
        self, workflow_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute the complete 4-phase incremental extraction flow.

        This is the default orchestration that apps can use directly:

            @workflow.run
            async def run(self, workflow_config: Dict[str, Any]) -> None:
                await self.run_incremental_extraction(workflow_config)

        Phases:
        1. Setup: get_workflow_args → fetch_incremental_marker → read_current_state
        2. Base Extraction: databases → schemas → tables → (columns if full) → transform
        3. Incremental Columns: prepare_queries → execute_batches (parallel) → transform
        4. Finalization: write_current_state → update_incremental_marker → upload

        Args:
            workflow_config: Initial workflow configuration

        Returns:
            Final workflow_args
        """
        # Phase 1: Setup
        workflow_args = await self.run_incremental_setup(workflow_config)

        # Phase 2: Base extraction
        workflow_args = await self.run_base_extraction(workflow_args)

        # Phase 3: Incremental columns (only if ready)
        if self.is_incremental_ready(workflow_args):
            workflow_args = await self.run_incremental_columns(workflow_args)

        # Phase 4: Finalization
        workflow_args = await self.run_incremental_finalization(workflow_args)

        logger.info("Incremental extraction workflow complete")
        return workflow_args


# Export for backward compatibility and discoverability
__all__ = ["IncrementalSQLWorkflowMixin"]
